# -*- coding: utf-8 -*-
"""网点价值与财务收支看板 - 数据聚合口径与评分模型
数据源: AnalyticDB sharing-citybike-pro / sharing-system-base-pro (经 electric/core/db)
单位: 库内金额为【分】, 对外统一转【元】
"""
import sys, time, datetime, os, json, hashlib
sys.path.insert(0, r"D:\Marvis K\janus\board-value")
sys.path.insert(0, r"D:\Marvis K\janus\electric")
from core import db
try:
    import config.settings as settings
    DB_BASE = settings.DB_BASE or 'sharing-system-base-pro'
except Exception:
    DB_BASE = 'sharing-system-base-pro'
try:
    from bvcore import demo
except Exception:
    import demo

# 网点价值全量聚合结果缓存 (5分钟TTL)
_SITE_VALUE_CACHE = {}
_SITE_VALUE_TTL = 300

# 数据来源标记: live=实时库 / cache=离线缓存(最后一次真实数据) / demo=演示数据
_LAST_SOURCE = 'live'

# ---------------- 落盘缓存(离线兜底): 断库/重启后展示最后一次真实数据 ----------------
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'cache')

def _cache_path(name):
    return os.path.join(_CACHE_DIR, name + '.json')

def _cache_save(name, payload):
    """保存最后一次真实数据到本地磁盘(断库兜底)"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(name), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        print('[cache_save]', name, e)
        return False

def _cache_load(name):
    """读取落盘缓存, 无/损坏返回 None"""
    try:
        with open(_cache_path(name), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _cache_key(*parts):
    """参数 -> 文件名片段(避免非法字符)"""
    raw = '|'.join(str(p) if p is not None else '' for p in parts)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]



def _db_ready():
    """数据库可用性探测：不可用(熔断/断连)时页面降级为演示数据
    BOARD_OFFLINE=1 可强制模拟离线(用于验证落盘缓存兜底)"""
    if os.environ.get('BOARD_OFFLINE') == '1':
        return False
    try:
        rows = db.query("SELECT 1 AS ok LIMIT 1", database=DB_BASE)
        return bool(rows)
    except Exception:
        return False

# ---------------- 码值映射 ----------------
SETTLE_WAY_MAP = {
    'online': '自动结算(柜机上报)',
    'offline': '手动抄表结算',
    'gdj_deduct': '供电局划扣(南方电网)',
    'not_settle': '不结算',
}
ALONE_METER_MAP = {
    'not_install': '柜内电表(无独立电表)',
    'only_install': '柜外独立电表(未用于结算)',
    'install_and_use': '柜外独立电表(用于结算)',
}
CYCLE_MAP = {1: '月结', 3: '季结', 6: '半年', 12: '一年'}
SITE_STATUS_MAP = {'on': '营业', 'off': '停用'}

def _y(v):
    """分 -> 元"""
    try:
        return round(float(v or 0) / 100.0, 2)
    except Exception:
        return 0.0

def _t(v):
    """毫秒时间戳 -> YYYY-MM-DD"""
    try:
        v = int(v or 0)
        if v <= 0:
            return ''
        return datetime.datetime.fromtimestamp(v / 1000).strftime('%Y-%m-%d')
    except Exception:
        return ''

def day_start_ms(date_str):
    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    return int(dt.timestamp() * 1000)

def day_after_ms(date_str):
    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d') + datetime.timedelta(days=1)
    return int(dt.timestamp() * 1000)

def default_window(days=90):
    now = datetime.datetime.now()
    d0 = (now - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    d1 = now.strftime('%Y-%m-%d')
    return d0, d1

# ---------------- 基础数据 ----------------
def load_agency_map():
    """代理商 id -> {name, level, parent_id, ...} (基础库)"""
    try:
        rows = db.query("SELECT id, name, level, parent_id, agency_status, is_del FROM sys_cm_agency WHERE is_del=0", database=DB_BASE)
    except Exception:
        rows = db.query("SELECT id, name, level, parent_id FROM sys_cm_agency", database=DB_BASE)
    m = {}
    for r in rows:
        m[int(r['id'])] = dict(r)
    return m

def load_merchant_map(ids=None):
    sql = "SELECT id, name, maker_id, lp_name, lp_phone FROM t_merchant WHERE is_del=0"
    if ids:
        sql += " AND id IN (%s)" % ','.join(str(int(x)) for x in ids)
    rows = db.query(sql)
    m = {}
    for r in rows:
        m[int(r['id'])] = dict(r)
    return m

def load_maker_phone_map(maker_ids=None):
    """创客 id -> 手机号 (取最新任务日志)"""
    if not maker_ids:
        return {}
    sql = ("SELECT t.maker_id, t.maker_phone FROM t_maker_earning_subsidy_task_log t "
           "JOIN (SELECT maker_id, MAX(id) mid FROM t_maker_earning_subsidy_task_log "
           "WHERE maker_phone IS NOT NULL AND maker_phone<>'' GROUP BY maker_id) x ON x.mid=t.id "
           "WHERE t.maker_id IN (%s)" % ','.join(str(int(x)) for x in maker_ids))
    m = {}
    try:
        for r in db.query(sql):
            m[int(r['maker_id'])] = r['maker_phone']
    except Exception:
        pass
    return m

def load_sites(agency_id=None, city=None, area=None, keyword=None, battery_product=None, site_status=None):
    """网点基础列表(含商户/创客手机号), 支持筛选; 返回 list[dict]"""
    conds = ["s.is_del=0"]
    if agency_id:
        conds.append("s.agency_id=%d" % int(agency_id))
    if city:
        conds.append("s.city='%s'" % city.replace("'", "''"))
    if area:
        conds.append("s.area='%s'" % area.replace("'", "''"))
    if battery_product:
        conds.append("s.battery_product_id=%d" % int(battery_product))
    if site_status:
        conds.append("s.site_status='%s'" % site_status)
    if keyword:
        conds.append("(s.name LIKE '%%%s%%' OR s.contact_person_name LIKE '%%%s%%')" % (keyword.replace("'", "''"), keyword.replace("'", "''")))
    sql = """SELECT s.id site_id, s.name site_name, s.type, s.city, s.area, s.street,
        s.agency_id, s.distributor_id, s.merchant_id, s.store_manager_name,
        s.battery_product_id, s.site_status, s.alone_meter_status, s.electric_settle_way,
        s.electric_settle_cycle, s.contact_person_name, s.creator_name, s.business_name
        FROM t_site s WHERE %s ORDER BY s.id""" % ' AND '.join(conds)
    rows = db.query(sql)
    merchant_ids = {int(r['merchant_id']) for r in rows if r.get('merchant_id')}
    mmap = load_merchant_map(merchant_ids)
    amap = load_agency_map()
    maker_ids = {int(mmap[int(mid)]['maker_id']) for mid in mmap if mmap[int(mid)].get('maker_id')}
    phtel = load_maker_phone_map(maker_ids)
    out = []
    for r in rows:
        mid = int(r['merchant_id']) if r.get('merchant_id') else None
        mr = mmap.get(mid, {})
        mk = int(mr.get('maker_id') or 0)
        ag = amap.get(int(r['agency_id']), {}) if r.get('agency_id') else {}
        out.append({
            'site_id': int(r['site_id']),
            'site_name': r['site_name'] or '',
            'city': r['city'] or '',
            'area': r['area'] or '',
            'street': r['street'] or '',
            'agency_id': int(r['agency_id']) if r.get('agency_id') else 0,
            'agency_name': ag.get('name') or '',
            'agency_level': ag.get('level') or '',
            'merchant_id': mid or 0,
            'merchant_name': mr.get('name') or '',
            'maker_id': mk,
            'maker_phone': phtel.get(mk) or '',
            'lp_phone': mr.get('lp_phone') or '',
            'battery_product_id': int(r['battery_product_id']) if r.get('battery_product_id') else 0,
            'site_status': r['site_status'] or '',
            'alone_meter_status': r['alone_meter_status'] or '',
            'electric_settle_way': r['electric_settle_way'] or '',
            'electric_settle_cycle': r['electric_settle_cycle'],
            'store_manager_name': r['store_manager_name'] or '',
        })
    return out

def load_battery_products():
    try:
        rows = db.query("SELECT id, name FROM t_battery_product WHERE is_del=0")
        return {int(r['id']): r['name'] for r in rows}
    except Exception:
        return {}

def _chunk_ids(ids, size=900):
    """分批, 规避 AnalyticDB IN 列表 4000 上限"""
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]

def _merge_site_agg(m, part):
    """合并分批聚合结果(不同聚合函数结构不同, 按字段融合)"""
    for k, v in part.items():
        if k not in m:
            m[k] = v
            continue
        d = m[k]
        if isinstance(v, dict):
            if 'items' in d and isinstance(d['items'], list) and 'items' in v:
                d['items'].extend(v['items'])
                for f in ('cnt', 'fee_in', 'fee_out', 'elec_fee'):
                    d[f] = (d.get(f) or 0) + (v.get(f) or 0)
            elif 'detail' in d and isinstance(d['detail'], dict) and 'detail' in v:
                d['cnt'] = (d.get('cnt') or 0) + (v.get('cnt') or 0)
                d['fee'] = (d.get('fee') or 0) + (v.get('fee') or 0)
                if v.get('price'):
                    d['price'] = v['price']
                for w, wv in v['detail'].items():
                    dw = d['detail'].setdefault(w, {'cnt': 0, 'fee': 0.0})
                    dw['cnt'] += wv['cnt']
                    dw['fee'] += wv['fee']
            else:
                for f in ('cnt', 'fee', 'refund_fee', 'power'):
                    if f in v and f in d:
                        d[f] = (d.get(f) or 0) + (v.get(f) or 0)
    return m

def _batched(fn, t0, t1, ids):
    if not ids or len(ids) <= 900:
        return fn(t0, t1, ids) or {}
    m = {}
    for chunk in _chunk_ids(ids):
        _merge_site_agg(m, fn(t0, t1, chunk) or {})
    return m

# ---------------- 收入侧聚合 ----------------
def agg_package_sales(t0, t1, site_ids=None):
    """电量套餐销售: package_order(source=buy, success) -> 服务单 sign_site_id"""
    conds = ["po.is_del=0", "po.order_status='success'", "po.source='buy'",
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)>=%d" % t0,
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)<%d" % t1]
    if site_ids:
        conds.append("so.sign_site_id IN (%s)" % ','.join(str(int(x)) for x in site_ids))
    sql = """SELECT so.sign_site_id site_id, COUNT(*) cnt, SUM(po.pay_fee) fee, SUM(po.refund_fee) refund_fee
        FROM t_user_exchange_package_order po
        JOIN t_exchange_service_order so ON so.id=po.exchange_service_order_id
        WHERE %s GROUP BY so.sign_site_id""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            m[int(r['site_id'])] = {'cnt': int(r['cnt'] or 0), 'fee': float(r['fee'] or 0), 'refund_fee': float(r['refund_fee'] or 0)}
    except Exception as e:
        print('[agg_package_sales]', e)
    return m

def agg_rent_sales(t0, t1, site_ids=None):
    """租金套餐销售: rent_package_order(success) -> 服务单 sign_site_id"""
    conds = ["po.is_del=0", "po.order_status='success'",
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)>=%d" % t0,
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)<%d" % t1]
    if site_ids:
        conds.append("so.sign_site_id IN (%s)" % ','.join(str(int(x)) for x in site_ids))
    sql = """SELECT so.sign_site_id site_id, COUNT(*) cnt, SUM(po.pay_fee) fee, SUM(po.refund_fee) refund_fee
        FROM t_user_exchange_rent_package_order po
        JOIN t_exchange_service_order so ON so.id=po.exchange_service_order_id
        WHERE %s GROUP BY so.sign_site_id""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            m[int(r['site_id'])] = {'cnt': int(r['cnt'] or 0), 'fee': float(r['fee'] or 0), 'refund_fee': float(r['refund_fee'] or 0)}
    except Exception as e:
        print('[agg_rent_sales]', e)
    return m

def agg_present_packages(t0, t1, site_ids=None):
    """赠送电量包: package_order(source in present/redemption/subsidy, success)
    估值 = 套餐模板 real_fee(分)"""
    conds = ["po.is_del=0", "po.order_status='success'", "po.source IN ('present','redemption','subsidy')",
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)>=%d" % t0,
             "COALESCE(NULLIF(po.pay_time,0),po.create_time)<%d" % t1]
    if site_ids:
        conds.append("so.sign_site_id IN (%s)" % ','.join(str(int(x)) for x in site_ids))
    sql = """SELECT so.sign_site_id site_id, COUNT(*) cnt, SUM(pk.real_fee) fee
        FROM t_user_exchange_package_order po
        JOIN t_exchange_service_order so ON so.id=po.exchange_service_order_id
        LEFT JOIN t_exchange_package pk ON pk.id=po.package_id
        WHERE %s GROUP BY so.sign_site_id""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            m[int(r['site_id'])] = {'cnt': int(r['cnt'] or 0), 'fee': float(r['fee'] or 0)}
    except Exception as e:
        print('[agg_present_packages]', e)
    return m

def agg_exchange_orders(t0, t1, site_ids=None):
    """换电订单: 成功换电; 金额=expend_power_fee(分) 电费口径; 电量=use_power(Wh)"""
    conds = ["is_del=0", "order_status='created'", "exchange_order_status='success'",
             "create_time>=%d" % t0, "create_time<%d" % t1]
    if site_ids:
        conds.append("site_id IN (%s)" % ','.join(str(int(x)) for x in site_ids))
    sql = """SELECT site_id, COUNT(*) cnt, SUM(expend_power_fee) fee, SUM(use_power) power
        FROM t_exchange_order WHERE %s GROUP BY site_id""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            m[int(r['site_id'])] = {'cnt': int(r['cnt'] or 0), 'fee': float(r['fee'] or 0), 'power': float(r['power'] or 0)}
    except Exception as e:
        print('[agg_exchange_orders]', e)
    return m

# ---------------- 支出侧聚合 ----------------
def agg_electric_settlement(t0, t1, site_ids=None):
    """电费结算单: t_exchange_electric_settlement"""
    conds = ["is_del=0", "create_time>=%d" % t0, "create_time<%d" % t1]
    if site_ids:
        conds.append("site_id IN (%s)" % ','.join(str(int(x)) for x in site_ids))
    sql = """SELECT site_id, settle_way, COUNT(*) cnt, SUM(settle_amount) fee, AVG(settle_price) price
        FROM t_exchange_electric_settlement WHERE %s GROUP BY site_id, settle_way""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            sid = int(r['site_id'])
            d = m.setdefault(sid, {'cnt': 0, 'fee': 0.0, 'price': 0.0, 'detail': {}})
            way = r['settle_way'] or ''
            d['detail'][way] = {'cnt': int(r['cnt'] or 0), 'fee': float(r['fee'] or 0)}
            d['cnt'] += int(r['cnt'] or 0)
            d['fee'] += float(r['fee'] or 0)
            if r.get('price'):
                d['price'] = float(r['price'])
    except Exception as e:
        print('[agg_electric_settlement]', e)
    return m

def agg_expense_bills(t0, t1, site_ids=None, agency_ids=None):
    """费用账单(已结算): 网点侧 = in_unit='swapSite' 或 out_unit='swapSite' 或 signSite"""
    conds = ["eb.bill_status='settle'", "eb.is_del=0",
             "eb.create_time>=%d" % t0, "eb.create_time<%d" % t1,
             "(eb.in_unit IN ('swapSite','signSite') OR eb.out_unit IN ('swapSite','signSite'))"]
    if site_ids:
        conds.append("(eb.in_unit_id IN (%s) OR eb.out_unit_id IN (%s))" % (
            ','.join(str(int(x)) for x in site_ids), ','.join(str(int(x)) for x in site_ids)))
    if agency_ids:
        conds.append("(eb.in_unit_id IN (%s) OR eb.out_unit_id IN (%s))" % (
            ','.join(str(int(x)) for x in agency_ids), ','.join(str(int(x)) for x in agency_ids)))
    sql = """SELECT CASE WHEN eb.in_unit IN ('swapSite','signSite') THEN eb.in_unit_id ELSE eb.out_unit_id END site_id,
        eb.expense_name, eb.expense_type, eb.expense_value, eb.expense_value_type, eb.standard_expense_desc,
        eb.in_unit, eb.out_unit,
        COUNT(*) cnt, SUM(eb.after_taxes_fee) fee
        FROM t_expense_bill eb WHERE %s
        GROUP BY site_id, eb.expense_name, eb.expense_type, eb.expense_value, eb.expense_value_type, eb.standard_expense_desc, eb.in_unit, eb.out_unit""" % ' AND '.join(conds)
    m = {}
    try:
        for r in db.query(sql):
            sid = int(r['site_id'])
            d = m.setdefault(sid, {'cnt': 0, 'fee_in': 0.0, 'fee_out': 0.0, 'elec_fee': 0.0, 'items': []})
            fee = float(r['fee'] or 0)
            d['cnt'] += int(r['cnt'] or 0)
            # 电费类费用(expense_value_type='electric' 或名称含'电费')与 settlement 同源, 单独统计避免重复计入成本
            is_elec = (r['expense_value_type'] == 'electric') or ('电费' in (r['expense_name'] or ''))
            # 网点为收款方 => 公司支付给网点(投入); 网点为付款方 => 网点退回/支出
            is_in = r['in_unit'] in ('swapSite', 'signSite')
            if is_elec:
                d['elec_fee'] += fee
            else:
                if is_in:
                    d['fee_in'] += fee
                else:
                    d['fee_out'] += fee
            d['items'].append({
                'expense_name': r['expense_name'] or '',
                'expense_type': r['expense_type'] or '',
                'expense_value': r['expense_value'],
                'expense_value_type': r['expense_value_type'] or '',
                'standard_expense_desc': r['standard_expense_desc'] or '',
                'in_unit': r['in_unit'] or '',
                'out_unit': r['out_unit'] or '',
                'cnt': int(r['cnt'] or 0),
                'fee': fee,
                'is_electric': is_elec,
            })
    except Exception as e:
        print('[agg_expense_bills]', e)
    return m

# ---------------- 网点用户数 / 设备数量(成本估算) ----------------
def agg_user_cnt(t0, t1, site_ids):
    """窗口内按网点去重的活跃用户数 (t_exchange_service_order 按 sign_site_id 去重 user_id)"""
    m = {}
    try:
        def _q(ids):
            inx = ','.join(str(int(x)) for x in ids)
            return db.query("""SELECT sign_site_id site_id, COUNT(DISTINCT buyer_user_id) cnt
                FROM t_exchange_service_order
                WHERE is_del=0 AND sign_site_id IN (%s)
                AND create_time>=%d AND create_time<%d
                GROUP BY sign_site_id""" % (inx, t0, t1))
        for r in _batched_rows(site_ids, _q):
            m[int(r['site_id'])] = int(r['cnt'] or 0)
    except Exception as e:
        print('[agg_user_cnt]', e)
    return m


def agg_device_cost(t0, t1, site_ids):
    """设备数量与成本估算: 换电柜 7000元/台, 电池 500元/颗
    主口径: t_site_device_statistics 快照(exchange_count=柜机数, store_have=在柜电池数)
    回退口径: t_exchange 计数 + t_battery_belong_relation(belong_type='exchange') 计数"""
    m = {int(sid): {'cabinet_cnt': 0, 'battery_cnt': 0} for sid in site_ids}
    ids = [int(x) for x in site_ids]
    got = False
    try:
        def _q_stat(part):
            inx = ','.join(str(int(x)) for x in part)
            return db.query("""SELECT site_id, exchange_count cab, store_have bat
                FROM t_site_device_statistics WHERE is_del=0 AND site_id IN (%s)""" % inx)
        for r in _batched_rows(ids, _q_stat):
            d = m.setdefault(int(r['site_id']), {'cabinet_cnt': 0, 'battery_cnt': 0})
            d['cabinet_cnt'] = int(r['cab'] or 0)
            d['battery_cnt'] = int(r['bat'] or 0)
            if d['cabinet_cnt'] or d['battery_cnt']:
                got = True
    except Exception as e:
        print('[agg_device_cost] stat', e)
    if not got:
        try:
            def _q_cab(part):
                inx = ','.join(str(int(x)) for x in part)
                return db.query("""SELECT site_id, COUNT(*) c FROM t_exchange
                    WHERE is_del=0 AND site_id IN (%s) GROUP BY site_id""" % inx)
            for r in _batched_rows(ids, _q_cab):
                m.setdefault(int(r['site_id']), {'cabinet_cnt': 0, 'battery_cnt': 0})['cabinet_cnt'] = int(r['c'] or 0)
        except Exception as e:
            print('[agg_device_cost] cabinet', e)
        try:
            def _q_bat(part):
                inx = ','.join(str(int(x)) for x in part)
                return db.query("""SELECT site_id, COUNT(*) c FROM t_battery_belong_relation
                    WHERE is_del=0 AND belong_type='exchange' AND site_id IN (%s) GROUP BY site_id""" % inx)
            for r in _batched_rows(ids, _q_bat):
                m.setdefault(int(r['site_id']), {'cabinet_cnt': 0, 'battery_cnt': 0})['battery_cnt'] = int(r['c'] or 0)
        except Exception as e:
            print('[agg_device_cost] battery', e)
    n_cab = sum(v['cabinet_cnt'] for v in m.values())
    n_bat = sum(v['battery_cnt'] for v in m.values())
    print('[agg_device_cost] 网点数=%d 柜机=%d 电池=%d 口径=%s' % (len(m), n_cab, n_bat, 'stat' if got else 'fallback'))
    return m


def _batched_rows(site_ids, fn, batch=900):
    """把 site_ids 分批执行 fn 并合并结果(规避 AnalyticDB IN 4000 上限)"""
    ids = [int(x) for x in site_ids]
    out = []
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        try:
            out.extend(fn(chunk) or [])
        except Exception as e:
            print('[batched_rows]', e)
    return out


# ---------------- 评分模型 ----------------
def calc_score(income, cost, orders, sales_fee):
    """综合评分(0-100) 与 等级
    income: 产出金额(元)  cost: 投入金额(元)  orders: 换电次数  sales_fee: 销售金额(元)"""
    if income <= 0 and cost <= 0:
        return 0, 'D', 0.0
    s1 = min(40.0, income / 10000.0 * 40.0)
    s2 = min(20.0, orders / 500.0 * 20.0)
    ratio = income / max(cost, 1.0) if cost > 0 else (40.0 if income > 0 else 0.0)
    if cost > 0 and income - cost < 0:
        s3 = max(0.0, min(40.0, (income / max(cost, 1.0)) * 8.0))
    else:
        s3 = min(40.0, ratio * 8.0)
    score = round(s1 + s2 + s3)
    if score >= 80:
        level = 'A'
    elif score >= 60:
        level = 'B'
    elif score >= 40:
        level = 'C'
    else:
        level = 'D'
    return score, level, round(ratio, 2)

# ---------------- 组装网点价值明细 ----------------
def build_site_value(agency_id=None, city=None, area=None, keyword=None, battery_product=None,
                     site_status=None, date_from=None, date_to=None, site_ids=None, merchant_key=None,
                     agency_key=None):
    if not _db_ready():
        # 断库/离线: 优先返回最后一次真实数据(落盘缓存), 无缓存再降级演示数据
        if not date_from or not date_to:
            d0, d1 = default_window(90)
            date_from = date_from or d0
            date_to = date_to or d1
        ck = _cache_key('site_value', agency_id, city, area, keyword, battery_product,
                        site_status, date_from, date_to, merchant_key, agency_key)
        hit = _cache_load('site_value_' + ck)
        if hit:
            global _LAST_SOURCE
            _LAST_SOURCE = 'cache'
            return hit['rows'], hit['date_from'], hit['date_to']
        _LAST_SOURCE = 'demo'
        return demo.build_site_value(date_from, date_to)
    t0 = day_start_ms(date_from) if date_from else None
    t1 = day_after_ms(date_to) if date_to else None
    if not t0 or not t1:
        d0, d1 = default_window(90)
        t0, t1 = day_start_ms(d0), day_after_ms(d1)
    if not date_from:
        date_from = datetime.datetime.fromtimestamp(t0 / 1000).strftime('%Y-%m-%d')
    if not date_to:
        date_to = datetime.datetime.fromtimestamp((t1 - 86400000) / 1000).strftime('%Y-%m-%d')

    # 5分钟窗口缓存, 避免 overview/finance_detail 等重复全量聚合
    _ck = (agency_id, city, area, keyword, battery_product, site_status, date_from, date_to,
           tuple(site_ids) if site_ids else None, merchant_key, agency_key)
    _hit = _SITE_VALUE_CACHE.get(_ck)
    if _hit and time.time() - _hit[0] < _SITE_VALUE_TTL:
        return _hit[1], date_from, date_to

    sites = load_sites(agency_id=agency_id, city=city, area=area, keyword=keyword,
                       battery_product=battery_product, site_status=site_status)
    if site_ids:
        sid_set = set(int(x) for x in site_ids)
        sites = [s for s in sites if s['site_id'] in sid_set]
    if merchant_key:
        sites = [s for s in sites if merchant_key in (s['merchant_name'] or '') or merchant_key in (s['maker_phone'] or '') or merchant_key in (s['lp_phone'] or '')]
    if agency_key:
        sites = [s for s in sites if agency_key in (s['agency_name'] or '')]
    if not sites:
        return [], date_from, date_to

    ids = [s['site_id'] for s in sites]
    bp = load_battery_products()
    pkg = _batched(agg_package_sales, t0, t1, ids)
    rent = _batched(agg_rent_sales, t0, t1, ids)
    pres = _batched(agg_present_packages, t0, t1, ids)
    exo = _batched(agg_exchange_orders, t0, t1, ids)
    elec = _batched(agg_electric_settlement, t0, t1, ids)
    bills = _batched(agg_expense_bills, t0, t1, ids)
    users = _batched(agg_user_cnt, t0, t1, ids)
    devs = _batched(agg_device_cost, t0, t1, ids)

    rows = []
    for s in sites:
        sid = s['site_id']
        p = pkg.get(sid, {'cnt': 0, 'fee': 0, 'refund_fee': 0})
        r_ = rent.get(sid, {'cnt': 0, 'fee': 0, 'refund_fee': 0})
        pr = pres.get(sid, {'cnt': 0, 'fee': 0})
        eo = exo.get(sid, {'cnt': 0, 'fee': 0, 'power': 0})
        el = elec.get(sid, {'cnt': 0, 'fee': 0, 'price': 0, 'detail': {}})
        bl = bills.get(sid, {'cnt': 0, 'items': [], 'fee_in': 0.0, 'fee_out': 0.0})

        fee_in = bl['fee_in']
        fee_out = bl['fee_out']
        elec_fee = el['fee']
        sales_fee = p['fee'] + r_['fee']
        exchange_fee = eo['fee']
        # 口径: 网点产出 = 销售金额 + 换电订单金额; 网点投入 = 电费结算 + 公司支付给网点的分成/补贴
        income = sales_fee + exchange_fee
        cost = elec_fee + fee_in

        score, level, ratio = calc_score(income, cost, eo['cnt'], sales_fee)
        price_y = round(el['price'] / 100.0, 4) if el['price'] else 0
        elec_detail = {SETTLE_WAY_MAP.get(k, k): {'cnt': v['cnt'], 'fee_y': _y(v['fee'])} for k, v in el['detail'].items()}
        share_items = []
        for it in bl['items']:
            if it['is_electric']:
                continue
            share_items.append('%s(%s,%s笔,%.2f元)' % (it['expense_name'], it['expense_value_type'], it['cnt'], _y(it['fee'])))
        # 结构化分成明细(名称/比例/金额)
        share_detail = []
        for it in bl['items']:
            if it['is_electric']:
                continue
            share_detail.append({
                'expense_name': it['expense_name'],
                'expense_type': it['expense_type'],
                'expense_value': it['expense_value'],
                'expense_value_type': it['expense_value_type'],
                'direction': 'in' if it['in_unit'] in ('swapSite', 'signSite') else 'out',
                'cnt': it['cnt'],
                'fee_y': _y(it['fee']),
            })
        # ---- 新增分析维度: 用户数/设备成本/激励补贴/业务员提成/电费拆分/净利 ----
        user_cnt = users.get(sid, 0)
        dev = devs.get(sid, {'cabinet_cnt': 0, 'battery_cnt': 0})
        cabinet_cnt = int(dev.get('cabinet_cnt', 0) or 0)
        battery_cnt = int(dev.get('battery_cnt', 0) or 0)
        device_total = cabinet_cnt * 7000 + battery_cnt * 500
        device_monthly = round(device_total / 36.0 + cabinet_cnt * 30.0, 2)
        promoter_fee = 0.0
        incentive = 0.0
        for it in bl['items']:
            if it['is_electric']:
                continue
            unit = (it['in_unit'] or '') + '|' + (it['out_unit'] or '')
            name = it['expense_name'] or ''
            if 'promoter' in unit or it['expense_type'] == 'promoter':
                promoter_fee += it['fee']
            if ('激励' in name or '补贴' in name) and it['in_unit'] in ('swapSite', 'signSite'):
                incentive += it['fee']
        sys_elec = el['detail'].get('online', {}).get('fee', 0)
        offline_transfer = el['detail'].get('offline', {}).get('fee', 0)
        gdj_deduct = el['detail'].get('gdj_deduct', {}).get('fee', 0)
        gross_profit = income - cost
        profit_margin = round(gross_profit / income * 100, 1) if income > 0 else 0.0
        net_profit = gross_profit - device_monthly * 100 - promoter_fee - pr['fee']
        rows.append({
            'date_from': date_from,
            'date_to': date_to,
            'city': s['city'],
            'area': s['area'],
            'street': s['street'],
            'agency_id': s['agency_id'],
            'agency_name': s['agency_name'],
            'agency_level': s['agency_level'],
            'site_id': sid,
            'site_name': s['site_name'],
            'site_status': SITE_STATUS_MAP.get(s['site_status'], s['site_status']),
            'merchant_id': s['merchant_id'],
            'merchant_name': s['merchant_name'],
            'maker_phone': s['maker_phone'],
            'lp_phone': s['lp_phone'],
            'store_manager_name': s['store_manager_name'],
            'battery_product': bp.get(s['battery_product_id'], str(s['battery_product_id'])),
            'meter_type': ALONE_METER_MAP.get(s['alone_meter_status'], s['alone_meter_status']),
            'settle_way': SETTLE_WAY_MAP.get(s['electric_settle_way'], s['electric_settle_way']),
            'settle_cycle': CYCLE_MAP.get(s['electric_settle_cycle'], ''),
            'electric_price_y': price_y,
            'electric_fee_y': _y(elec_fee),
            'electric_cnt': el['cnt'],
            'electric_detail': elec_detail,
            'share_cnt': bl['cnt'],
            'share_fee_y': _y(fee_in + fee_out),
            'share_fee_in_y': _y(fee_in),
            'share_fee_out_y': _y(fee_out),
            'share_items': share_items,
            'share_detail': share_detail,
            'order_cnt': eo['cnt'],
            'order_fee_y': _y(eo['fee']),
            'use_power_wh': round(eo['power'], 0),
            'sales_cnt': p['cnt'] + r_['cnt'],
            'sales_fee_y': _y(sales_fee),
            'refund_fee_y': _y(p['refund_fee'] + r_['refund_fee']),
            'present_cnt': pr['cnt'],
            'present_fee_y': _y(pr['fee']),
            'income_y': round(income / 100.0, 2),
            'cost_y': round(cost / 100.0, 2),
            'profit_y': round((income - cost) / 100.0, 2),
            'ratio': ratio,
            'score': score,
            'level': level,
            'user_cnt': user_cnt,
            'incentive_y': _y(incentive),
            'promoter_fee_y': _y(promoter_fee),
            'sys_elec_y': _y(sys_elec),
            'offline_transfer_y': _y(offline_transfer),
            'gdj_deduct_y': _y(gdj_deduct),
            'device_cabinet_cnt': cabinet_cnt,
            'device_battery_cnt': battery_cnt,
            'device_cost_total_y': round(device_total, 2),
            'device_cost_monthly_y': device_monthly,
            'profit_margin': profit_margin,
            'net_profit_y': round(net_profit / 100.0, 2),
        })
    _LAST_SOURCE = 'live'
    _SITE_VALUE_CACHE[_ck] = (time.time(), rows)
    _cache_save('site_value_' + _cache_key('site_value', agency_id, city, area, keyword, battery_product,
                                           site_status, date_from, date_to, merchant_key, agency_key),
                {'rows': rows, 'date_from': date_from, 'date_to': date_to, 'saved_at': time.time()})
    return rows, date_from, date_to

# ---------------- 财务收支汇总(代理商/费用类别/趋势) ----------------
def build_finance_summary(date_from=None, date_to=None):
    global _LAST_SOURCE
    if not _db_ready():
        if not date_from or not date_to:
            d0, d1 = default_window(90)
            date_from = date_from or d0
            date_to = date_to or d1
        ck = _cache_key('finance_summary', date_from, date_to)
        hit = _cache_load('finance_summary_' + ck)
        if hit:
            _LAST_SOURCE = 'cache'
            return hit['rows'], hit['date_from'], hit['date_to']
        _LAST_SOURCE = 'demo'
        return demo.build_finance_summary(date_from, date_to)
    t0 = day_start_ms(date_from) if date_from else None
    t1 = day_after_ms(date_to) if date_to else None
    if not t0 or not t1:
        d0, d1 = default_window(90)
        t0, t1 = day_start_ms(d0), day_after_ms(d1)
    if not date_from:
        date_from = datetime.datetime.fromtimestamp(t0 / 1000).strftime('%Y-%m-%d')
    if not date_to:
        date_to = datetime.datetime.fromtimestamp((t1 - 86400000) / 1000).strftime('%Y-%m-%d')

    amap = load_agency_map()
    conds = ["eb.bill_status='settle'", "eb.is_del=0", "eb.create_time>=%d" % t0, "eb.create_time<%d" % t1,
             "(eb.in_unit IN ('swapAgency','signAgency') OR eb.out_unit IN ('swapAgency','signAgency'))"]
    rows = db.query("""SELECT eb.in_unit, eb.in_unit_id, eb.in_unit_name,
        eb.out_unit, eb.out_unit_id, eb.out_unit_name,
        eb.expense_name, eb.expense_type, COUNT(*) cnt, SUM(eb.after_taxes_fee) fee
        FROM t_expense_bill eb WHERE %s GROUP BY eb.in_unit, eb.in_unit_id, eb.in_unit_name,
        eb.out_unit, eb.out_unit_id, eb.out_unit_name, eb.expense_name, eb.expense_type""" % ' AND '.join(conds))
    bill_rows = []
    for r in rows:
        fee = float(r['fee'] or 0)
        # 资金方向: in_unit=收款方 / out_unit=付款方
        if r['in_unit'] in ('swapAgency', 'signAgency'):
            aid = int(r['in_unit_id'])
            direction = 'in'
            aname = r['in_unit_name'] or ''
        elif r['out_unit'] in ('swapAgency', 'signAgency'):
            aid = int(r['out_unit_id'])
            direction = 'out'
            aname = r['out_unit_name'] or ''
        else:
            continue
        bill_rows.append({
            'agency_id': aid,
            'agency_name': amap.get(aid, {}).get('name') or aname or str(aid),
            'expense_name': r['expense_name'] or '',
            'expense_type': r['expense_type'] or '',
            'cnt': int(r['cnt'] or 0),
            'fee': fee,
            'direction': direction,
        })
    elec_rows = db.query("""SELECT s.agency_id, e.settle_way, COUNT(*) cnt, SUM(e.settle_amount) fee
        FROM t_exchange_electric_settlement e JOIN t_site s ON s.id=e.site_id
        WHERE e.is_del=0 AND e.create_time>=%d AND e.create_time<%d
        GROUP BY s.agency_id, e.settle_way""" % (t0, t1))
    sale_rows = db.query("""SELECT s.agency_id, COUNT(*) cnt, SUM(po.pay_fee) fee
        FROM t_user_exchange_package_order po
        JOIN t_exchange_service_order so ON so.id=po.exchange_service_order_id
        JOIN t_site s ON s.id=so.sign_site_id
        WHERE po.is_del=0 AND po.order_status='success' AND po.source='buy'
        AND COALESCE(NULLIF(po.pay_time,0),po.create_time)>=%d AND COALESCE(NULLIF(po.pay_time,0),po.create_time)<%d
        GROUP BY s.agency_id""" % (t0, t1))
    rent_rows = db.query("""SELECT s.agency_id, COUNT(*) cnt, SUM(po.pay_fee) fee
        FROM t_user_exchange_rent_package_order po
        JOIN t_exchange_service_order so ON so.id=po.exchange_service_order_id
        JOIN t_site s ON s.id=so.sign_site_id
        WHERE po.is_del=0 AND po.order_status='success'
        AND COALESCE(NULLIF(po.pay_time,0),po.create_time)>=%d AND COALESCE(NULLIF(po.pay_time,0),po.create_time)<%d
        GROUP BY s.agency_id""" % (t0, t1))
    exch_rows = db.query("""SELECT agency_id, COUNT(*) cnt, SUM(expend_power_fee) fee
        FROM t_exchange_order WHERE is_del=0 AND order_status='created' AND exchange_order_status='success'
        AND create_time>=%d AND create_time<%d GROUP BY agency_id""" % (t0, t1))

    agency_agg = {}
    for r in bill_rows:
        d = agency_agg.setdefault(r['agency_id'], {'agency_id': r['agency_id'], 'agency_name': r['agency_name'],
            'income_y': 0.0, 'cost_y': 0.0, 'bill_out_y': 0.0, 'bill_in_y': 0.0, 'elec_y': 0.0,
            'sale_y': 0.0, 'order_y': 0.0, 'by_cate': {}})
        if r['direction'] == 'out':
            d['cost_y'] += r['fee'] / 100.0
            d['bill_out_y'] += r['fee'] / 100.0
        else:
            d['income_y'] += r['fee'] / 100.0
            d['bill_in_y'] += r['fee'] / 100.0
        key = r['expense_name'] or '其他'
        c = d['by_cate'].setdefault(key, {'out': 0.0, 'in': 0.0, 'cnt': 0})
        c['cnt'] += r['cnt']
        if r['direction'] == 'out':
            c['out'] += r['fee'] / 100.0
        else:
            c['in'] += r['fee'] / 100.0
    for r in elec_rows:
        d = agency_agg.setdefault(int(r['agency_id']), {'agency_id': int(r['agency_id']), 'agency_name': amap.get(int(r['agency_id']), {}).get('name') or str(r['agency_id']),
            'income_y': 0.0, 'cost_y': 0.0, 'bill_out_y': 0.0, 'bill_in_y': 0.0, 'elec_y': 0.0, 'sale_y': 0.0, 'order_y': 0.0, 'by_cate': {}})
        fee = float(r['fee'] or 0) / 100.0
        d['cost_y'] += fee
        d['elec_y'] += fee
    for r in sale_rows + rent_rows:
        d = agency_agg.setdefault(int(r['agency_id']), {'agency_id': int(r['agency_id']), 'agency_name': amap.get(int(r['agency_id']), {}).get('name') or str(r['agency_id']),
            'income_y': 0.0, 'cost_y': 0.0, 'bill_out_y': 0.0, 'bill_in_y': 0.0, 'elec_y': 0.0, 'sale_y': 0.0, 'order_y': 0.0, 'by_cate': {}})
        d['income_y'] += float(r['fee'] or 0) / 100.0
        d['sale_y'] += float(r['fee'] or 0) / 100.0
    for r in exch_rows:
        d = agency_agg.setdefault(int(r['agency_id']), {'agency_id': int(r['agency_id']), 'agency_name': amap.get(int(r['agency_id']), {}).get('name') or str(r['agency_id']),
            'income_y': 0.0, 'cost_y': 0.0, 'bill_out_y': 0.0, 'bill_in_y': 0.0, 'elec_y': 0.0, 'sale_y': 0.0, 'order_y': 0.0, 'by_cate': {}})
        d['income_y'] += float(r['fee'] or 0) / 100.0
        d['order_y'] += float(r['fee'] or 0) / 100.0

    result = []
    for d in agency_agg.values():
        d['income_y'] = round(d['income_y'], 2)
        d['cost_y'] = round(d['cost_y'], 2)
        d['profit_y'] = round(d['income_y'] - d['cost_y'], 2)
        d['ratio'] = round(d['income_y'] / d['cost_y'], 2) if d['cost_y'] > 0 else 0
        cate = []
        for k, v in d['by_cate'].items():
            cate.append({'name': k, 'out_y': round(v['out'], 2), 'in_y': round(v['in'], 2), 'cnt': v['cnt']})
        d['by_cate'] = sorted(cate, key=lambda x: -(x['out_y'] + x['in_y']))
        result.append(d)
    result.sort(key=lambda x: -x['cost_y'])
    _cache_save('finance_summary_' + _cache_key('finance_summary', date_from, date_to),
                {'rows': result, 'date_from': date_from, 'date_to': date_to, 'saved_at': time.time()})
    return result, date_from, date_to

# ---------------- 费用明细账单(每代理商收支及费用明细) ----------------
def build_expense_bills(agency_id=None, date_from=None, date_to=None, keyword=None, page=1, page_size=50,
                        bill_id=None, business_id=None, site_name=None, merchant_name=None, city=None):
    global _LAST_SOURCE
    if not _db_ready():
        if not date_from or not date_to:
            d0, d1 = default_window(90)
            date_from = date_from or d0
            date_to = date_to or d1
        # 离线: 读取最后一次连库时的账单页缓存(键忽略分页参数, 按当前分页重新切片)
        ck = _cache_key('expense_bills', agency_id, date_from, date_to, keyword)
        hit = _cache_load('expense_bills_' + ck)
        if hit:
            _LAST_SOURCE = 'cache'
            rows = hit['rows']
            total = hit['total']
            page = max(1, int(page))
            page_size = max(1, int(page_size))
            if page_size <= 0:
                return rows, total, hit['date_from'], hit['date_to']
            start = (page - 1) * page_size
            if start >= len(rows):
                start = 0  # 离线缓存仅存最后一次连库的单页, 越界时回退到缓存页避免白屏
            return rows[start:start + page_size], total, hit['date_from'], hit['date_to']
        _LAST_SOURCE = 'demo'
        return demo.build_expense_bills(agency_id, date_from, date_to, keyword, page, page_size)
    t0 = day_start_ms(date_from) if date_from else None
    t1 = day_after_ms(date_to) if date_to else None
    if not t0 or not t1:
        d0, d1 = default_window(90)
        t0, t1 = day_start_ms(d0), day_after_ms(d1)
    if not date_from:
        date_from = datetime.datetime.fromtimestamp(t0 / 1000).strftime('%Y-%m-%d')
    if not date_to:
        date_to = datetime.datetime.fromtimestamp((t1 - 86400000) / 1000).strftime('%Y-%m-%d')

    amap = load_agency_map()
    conds = ["eb.bill_status='settle'", "eb.is_del=0", "eb.create_time>=%d" % t0, "eb.create_time<%d" % t1]
    if agency_id:
        conds.append("(eb.in_unit_id=%d OR eb.out_unit_id=%d)" % (int(agency_id), int(agency_id)))
    if bill_id:
        conds.append("eb.id=%d" % int(bill_id))
    if business_id:
        conds.append("eb.business_id=%d" % int(business_id))
    if site_name:
        kw = site_name.replace("'", "''")
        conds.append("(eb.in_unit_name LIKE '%%%s%%' OR eb.out_unit_name LIKE '%%%s%%')" % (kw, kw))
    if merchant_name:
        kw = merchant_name.replace("'", "''")
        conds.append("(eb.in_unit_name LIKE '%%%s%%' OR eb.out_unit_name LIKE '%%%s%%')" % (kw, kw))
    if city:
        kw = city.replace("'", "''")
        conds.append("EXISTS (SELECT 1 FROM t_site s WHERE s.id IN (eb.in_unit_id, eb.out_unit_id) AND s.city='%s')" % kw)
    if keyword:
        kw = keyword.replace("'", "''")
        conds.append("(eb.expense_name LIKE '%%%s%%' OR eb.in_unit_name LIKE '%%%s%%' OR eb.out_unit_name LIKE '%%%s%%')" % (kw, kw, kw))
    base = "FROM t_expense_bill eb WHERE %s" % ' AND '.join(conds)
    total = db.query("SELECT COUNT(*) c " + base)[0]['c']
    if page_size <= 0:
        limit_sql = ""
    else:
        offset = (page - 1) * page_size
        limit_sql = " LIMIT %d OFFSET %d" % (page_size, offset)
    rows = db.query("""SELECT eb.id bill_id, eb.business_type, eb.business_id, eb.expense_name, eb.expense_type,
        eb.expense_value, eb.expense_value_type, eb.standard_expense_desc, eb.fee, eb.after_taxes_fee,
        eb.in_unit, eb.in_unit_id, eb.in_unit_name, eb.out_unit, eb.out_unit_id, eb.out_unit_name,
        eb.create_time, eb.settle_time, eb.bill_status
        %s ORDER BY eb.create_time DESC%s""" % (base, limit_sql))
    out = []
    for r in rows:
        aid = 0
        if r['in_unit'] in ('swapAgency', 'signAgency'):
            aid = int(r['in_unit_id'])
        elif r['out_unit'] in ('swapAgency', 'signAgency'):
            aid = int(r['out_unit_id'])
        out.append({
            'bill_id': r['bill_id'],
            'bill_date': _t(r['create_time']),
            'settle_date': _t(r['settle_time']),
            'business_type': r['business_type'] or '',
            'business_id': r['business_id'],
            'expense_name': r['expense_name'] or '',
            'expense_type': r['expense_type'] or '',
            'expense_value': r['expense_value'],
            'expense_value_type': r['expense_value_type'] or '',
            'standard_expense_desc': r['standard_expense_desc'] or '',
            'fee_y': _y(r['fee']),
            'after_taxes_fee_y': _y(r['after_taxes_fee']),
            'in_unit': r['in_unit'] or '',
            'in_unit_id': r['in_unit_id'],
            'in_unit_name': r['in_unit_name'] or '',
            'out_unit': r['out_unit'] or '',
            'out_unit_id': r['out_unit_id'],
            'out_unit_name': r['out_unit_name'] or '',
            'agency_id': aid,
            'agency_name': amap.get(aid, {}).get('name') if aid else '',
        })
    _cache_save('expense_bills_' + _cache_key('expense_bills', agency_id, date_from, date_to, keyword),
                {'rows': out, 'total': total, 'date_from': date_from, 'date_to': date_to, 'saved_at': time.time()})
    return out, total, date_from, date_to

# ---------------- 总看板(收入支出结构/趋势/最有价值网点) ----------------
def build_overview(date_from=None, date_to=None):
    """总看板: 各维度财务收入支出 + 最具价值网点TOP
    真实库直拉聚合; 库不可用时自动降级演示数据"""
    global _LAST_SOURCE
    if not _db_ready():
        if not date_from or not date_to:
            d0, d1 = default_window(90)
            date_from = date_from or d0
            date_to = date_to or d1
        ck = _cache_key('overview', date_from, date_to)
        hit = _cache_load('overview_' + ck)
        if hit:
            _LAST_SOURCE = 'cache'
            hit['data']['data_date'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(hit.get('saved_at', time.time())))
            return hit['data']
        _LAST_SOURCE = 'demo'
        return demo.build_overview(date_from, date_to)
    t0 = day_start_ms(date_from) if date_from else None
    t1 = day_after_ms(date_to) if date_to else None
    if not t0 or not t1:
        d0, d1 = default_window(90)
        t0, t1 = day_start_ms(d0), day_after_ms(d1)
    if not date_from:
        date_from = datetime.datetime.fromtimestamp(t0 / 1000).strftime('%Y-%m-%d')
    if not date_to:
        date_to = datetime.datetime.fromtimestamp((t1 - 86400000) / 1000).strftime('%Y-%m-%d')

    sites, df, dt = build_site_value(date_from=date_from, date_to=date_to)
    fin, _, _ = build_finance_summary(date_from, date_to)
    total_income = round(sum(s['income_y'] for s in sites), 2)
    total_cost = round(sum(s['cost_y'] for s in sites), 2)
    sys_elec = round(sum(s['sys_elec_y'] for s in sites), 2)
    offline_elec = round(sum(s['offline_transfer_y'] for s in sites), 2)
    gdj_elec = round(sum(s['gdj_deduct_y'] for s in sites), 2)
    share_out = round(sum(s['share_fee_in_y'] for s in sites), 2)
    share_in = round(sum(s['share_fee_out_y'] for s in sites), 2)
    promo = round(sum(s['promoter_fee_y'] for s in sites), 2)
    present_v = round(sum(s['present_fee_y'] for s in sites), 2)
    present_cnt = sum(s['present_cnt'] for s in sites)
    device_cabinet = sum(s['device_cabinet_cnt'] for s in sites)
    device_battery = sum(s['device_battery_cnt'] for s in sites)
    device_total = round(device_cabinet * 7000 + device_battery * 500, 2)
    order_cnt = sum(s['order_cnt'] for s in sites)
    sales_y = round(sum(s['sales_fee_y'] for s in sites), 2)
    order_y = round(sum(s['order_fee_y'] for s in sites), 2)
    user_cnt = sum(s['user_cnt'] for s in sites)
    kpi = {
        'income_y': total_income, 'cost_y': total_cost, 'profit_y': round(total_income - total_cost, 2),
        'order_cnt': order_cnt, 'sales_y': sales_y, 'order_y': order_y, 'user_cnt': user_cnt,
        'elec_y': round(sys_elec + offline_elec + gdj_elec, 2),
        'sys_elec_y': sys_elec, 'offline_elec_y': offline_elec, 'gdj_elec_y': gdj_elec,
        'share_out_y': share_out, 'share_in_y': share_in, 'promoter_fee_y': promo,
        'present_cnt': present_cnt, 'present_value_y': present_v,
        'device_cabinet_cnt': device_cabinet, 'device_battery_cnt': device_battery,
        'device_cost_y': device_total,
    }
    income_breakdown = [
        {'name': '销售金额', 'value_y': sales_y},
        {'name': '换电订单金额', 'value_y': order_y},
        {'name': '分成/补贴收入', 'value_y': share_in},
    ]
    cost_breakdown = [
        {'name': '网点电费', 'value_y': kpi['elec_y']},
        {'name': '分成/补贴支出', 'value_y': share_out},
        {'name': '业务员提成', 'value_y': promo},
        {'name': '赠送电量包(客情)', 'value_y': present_v},
        {'name': '设备投入(柜+电池)', 'value_y': device_total},
    ]
    trend = _overview_trend(t0, t1)
    agency_top = []
    for d in fin:
        agency_top.append({'agency_id': d['agency_id'], 'agency_name': d['agency_name'],
                           'income_y': d['income_y'], 'cost_y': d['cost_y'], 'profit_y': d['profit_y'],
                           'score': d.get('score', 0)})
    top_sites = [{'site_id': s['site_id'], 'site_name': s['site_name'], 'city': s['city'],
                  'agency_name': s['agency_name'], 'income_y': s['income_y'], 'profit_y': s['profit_y'],
                  'score': s['score'], 'level': s['level']} for s in sites[:10]]
    score_dist = {'A': sum(1 for s in sites if s['level'] == 'A'),
                  'B': sum(1 for s in sites if s['level'] == 'B'),
                  'C': sum(1 for s in sites if s['level'] == 'C'),
                  'D': sum(1 for s in sites if s['level'] == 'D')}
    ret = {'kpi': kpi, 'income_breakdown': income_breakdown, 'cost_breakdown': cost_breakdown,
           'trend': trend, 'agency_top': agency_top, 'top_sites': top_sites,
           'score_dist': score_dist, 'site_cnt': len(sites),
           'date_from': date_from, 'date_to': date_to}
    _cache_save('overview_' + _cache_key('overview', date_from, date_to), {'data': ret, 'saved_at': time.time()})
    return ret


def _overview_trend(t0, t1):
    """按日收支趋势(费用账单+电费), 失败返回空"""
    try:
        bill = db.query("""SELECT DATE(FROM_UNIXTIME(create_time/1000)) d,
            SUM(CASE WHEN in_unit IN ('swapAgency','signAgency') THEN after_taxes_fee ELSE 0 END) in_fee,
            SUM(CASE WHEN out_unit IN ('swapAgency','signAgency') THEN after_taxes_fee ELSE 0 END) out_fee
            FROM t_expense_bill WHERE is_del=0 AND bill_status='settle'
            AND create_time>=%d AND create_time<%d GROUP BY d ORDER BY d""" % (t0, t1))
        elec = db.query("""SELECT DATE(FROM_UNIXTIME(create_time/1000)) d, SUM(settle_amount) fee
            FROM t_exchange_electric_settlement WHERE is_del=0
            AND create_time>=%d AND create_time<%d GROUP BY d ORDER BY d""" % (t0, t1))
        elec_map = {}
        for r in elec:
            elec_map[r['d'].isoformat() if hasattr(r['d'], 'isoformat') else str(r['d'])] = float(r['fee'] or 0) / 100.0
        out = []
        for r in bill:
            d = r['d'].isoformat() if hasattr(r['d'], 'isoformat') else str(r['d'])
            in_fee = float(r['in_fee'] or 0) / 100.0
            out_fee = float(r['out_fee'] or 0) / 100.0
            out.append({'date': d, 'income_y': round(in_fee, 2),
                        'cost_y': round(out_fee + elec_map.get(d, 0), 2),
                        'profit_y': round(in_fee - out_fee - elec_map.get(d, 0), 2)})
        return out
    except Exception as e:
        print('[overview_trend]', e)
        return []


# ---------------- 财务看板(成本费用结构明细) ----------------
def build_finance_detail(date_from=None, date_to=None):
    """财务看板: 公司支出/网点收入/设备/软硬件/运营/员工提成/电费/分成/赠送包"""
    global _LAST_SOURCE
    if not _db_ready():
        if not date_from or not date_to:
            d0, d1 = default_window(90)
            date_from = date_from or d0
            date_to = date_to or d1
        ck = _cache_key('finance_detail', date_from, date_to)
        hit = _cache_load('finance_detail_' + ck)
        if hit:
            _LAST_SOURCE = 'cache'
            hit['data']['data_date'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(hit.get('saved_at', time.time())))
            return hit['data']
        _LAST_SOURCE = 'demo'
        return demo.build_finance_detail(date_from, date_to)
    sites, df, dt = build_site_value(date_from=date_from, date_to=date_to)
    sys_elec = round(sum(s['sys_elec_y'] for s in sites), 2)
    offline_elec = round(sum(s['offline_transfer_y'] for s in sites), 2)
    gdj_elec = round(sum(s['gdj_deduct_y'] for s in sites), 2)
    share_out = round(sum(s['share_fee_in_y'] for s in sites), 2)
    promo = round(sum(s['promoter_fee_y'] for s in sites), 2)
    present_v = round(sum(s['present_fee_y'] for s in sites), 2)
    present_cnt = sum(s['present_cnt'] for s in sites)
    device_cabinet = sum(s['device_cabinet_cnt'] for s in sites)
    device_battery = sum(s['device_battery_cnt'] for s in sites)
    device_cost = round(device_cabinet * 7000 + device_battery * 500, 2)
    revenue = round(sum(s['income_y'] for s in sites), 2)
    sales_y = round(sum(s['sales_fee_y'] for s in sites), 2)
    order_y = round(sum(s['order_fee_y'] for s in sites), 2)
    software_y = round(revenue * 0.02, 2)
    hardware_other_y = round(device_cost * 0.05, 2)
    operate_y = round(revenue * 0.03, 2)
    total_expense = round(sys_elec + offline_elec + gdj_elec + share_out + promo + present_v + device_cost + software_y + hardware_other_y + operate_y, 2)
    summary = {
        'revenue_y': revenue, 'sales_y': sales_y, 'order_y': order_y,
        'elec_total_y': round(sys_elec + offline_elec + gdj_elec, 2),
        'sys_elec_y': sys_elec, 'offline_elec_y': offline_elec, 'gdj_elec_y': gdj_elec,
        'share_out_y': share_out, 'promoter_fee_y': promo,
        'present_cnt': present_cnt, 'present_value_y': present_v,
        'device_cabinet_cnt': device_cabinet, 'device_battery_cnt': device_battery,
        'device_cost_y': device_cost,
        'software_y': software_y, 'hardware_other_y': hardware_other_y, 'operate_y': operate_y,
        'total_expense_y': total_expense,
        'net_profit_y': round(revenue - total_expense, 2),
    }
    items = [
        {'cate': '营收', 'name': '销售金额', 'value_y': sales_y, 'kind': 'income'},
        {'cate': '营收', 'name': '换电订单金额', 'value_y': order_y, 'kind': 'income'},
        {'cate': '设备投入', 'name': '换电柜(7000元/台×%d台)' % device_cabinet, 'value_y': round(device_cabinet * 7000, 2), 'kind': 'cost'},
        {'cate': '设备投入', 'name': '电池(500元/颗×%d颗)' % device_battery, 'value_y': round(device_battery * 500, 2), 'kind': 'cost'},
        {'cate': '软硬件投入', 'name': '软硬件其他投入(估)', 'value_y': round(software_y + hardware_other_y, 2), 'kind': 'cost'},
        {'cate': '运营投入', 'name': '运营投入(估)', 'value_y': operate_y, 'kind': 'cost'},
        {'cate': '员工成本', 'name': '业务员提成/佣金', 'value_y': promo, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '系统结算电费(自动)', 'value_y': sys_elec, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '线下结算-手动抄表(对公)', 'value_y': offline_elec, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '线下结算-供电局划扣', 'value_y': gdj_elec, 'kind': 'cost'},
        {'cate': '分成支出', 'name': '公司支付给网点的分成/补贴', 'value_y': share_out, 'kind': 'cost'},
        {'cate': '客情维护', 'name': '赠送电量包(%d笔)' % present_cnt, 'value_y': present_v, 'kind': 'cost'},
    ]
    ret = {'summary': summary, 'items': items, 'date_from': df, 'date_to': dt}
    _cache_save('finance_detail_' + _cache_key('finance_detail', df, dt), {'data': ret, 'saved_at': time.time()})
    return ret
