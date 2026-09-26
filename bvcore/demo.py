# -*- coding: utf-8 -*-
"""演示数据模块：数据库不可用时返回仿真数据，保证页面可开发调试。
明天连库后真实逻辑走 metrics.build_* 的 SQL 分支，本模块仅作离线兜底，
所有金额单位均为【元】（与对外口径一致）。
"""
import random, datetime

random.seed(20260916)

AGENCIES = [
    {'id': 24071055, 'name': '龙华代理商', 'level': '一级'},
    {'id': 24071056, 'name': '福田代理商', 'level': '一级'},
    {'id': 24071057, 'name': '南山代理商', 'level': '一级'},
    {'id': 24072001, 'name': '杭州滨江代理商', 'level': '一级'},
    {'id': 24072002, 'name': '阳朔代理商', 'level': '二级'},
    {'id': 24073001, 'name': '广州白云代理商', 'level': '一级'},
]

CITIES = [
    ('深圳', ['龙华区', '福田区', '南山区'], ['民治街道', '华强北街道', '科技园街道', '香蜜湖街道']),
    ('杭州', ['滨江区', '西湖区'], ['西兴街道', '文三路街道']),
    ('阳朔', ['阳朔县'], ['阳朔镇', '白沙镇']),
    ('广州', ['白云区'], ['同和街道', '三元里街道']),
]

SETTLE_WAYS = [('online', '自动结算(柜机上报)', 0.6), ('offline', '手动抄表结算', 0.8), ('gdj_deduct', '供电局划扣(南方电网)', 0.75)]
BATTERY_PRODUCTS = ['48V20Ah', '48V30Ah', '60V20Ah', '72V20Ah']
MERCHANTS = [('深圳创客01', '138****4d00'), ('好运来便利店', '138****cd96'), ('阳光超市', '138****e910'),
             ('老张车行', '138****5fb3'), ('捷安特专卖店', '138****4c68'), ('绿源电动车行', '138****6592'),
             ('滴滴换电合作店', '138****41f8'), ('顺丰驿站', '138****5e46')]
EXPENSE_NAMES = ['网点柜机电费', '落柜场地换电分成', '用户拓展换电分成', '换电分成', '商户提现',
                 '激励补贴', '电费补贴', '销售补贴', '用户推广佣金', '推广费提成', '场地租金补贴']


def _rnd(a, b):
    return round(random.uniform(a, b), 2)


def _int_rnd(a, b):
    return random.randint(a, b)


def _mk_sites():
    sites = []
    sid = 100001
    for (city, areas, streets) in CITIES:
        n_area = len(areas)
        for i in range(_int_rnd(6, 12)):
            agency = random.choice(AGENCIES)
            m_name, m_phone = random.choice(MERCHANTS)
            settle, settle_name, price = random.choice(SETTLE_WAYS)
            sites.append({
                'site_id': sid,
                'site_name': '%s换电柜-%04d' % (city, sid - 100000),
                'city': city,
                'area': areas[i % n_area],
                'street': random.choice(streets),
                'agency_id': agency['id'],
                'agency_name': agency['name'],
                'agency_level': agency['level'],
                'merchant_id': 500000 + sid,
                'merchant_name': m_name,
                'maker_phone': m_phone,
                'lp_phone': '139%s' % str(_int_rnd(10000000, 99999999)),
                'store_manager_name': '店长%d' % _int_rnd(1, 99),
                'battery_product': random.choice(BATTERY_PRODUCTS),
                'meter_type': random.choice(['柜内电表(无独立电表)', '柜外独立电表(用于结算)', '柜外独立电表(未用于结算)']),
                'settle_way': settle_name,
                'settle_cycle': random.choice(['月结', '季结', '年结', '']),
                'electric_price_y': price,
                'site_status': random.choice(['营业', '营业', '营业', '停用']),
            })
            sid += 1
    return sites


def _mk_electric_detail(settle_way):
    """系统电费 / 线下结算金额拆分"""
    total = _rnd(800, 6000)
    sys_elec = _rnd(300, 3000)
    offline_transfer = _rnd(100, 2000)
    gdj_deduct = _rnd(100, 1800)
    if settle_way == '自动结算(柜机上报)':
        sys_elec = total
        offline_transfer = 0
        gdj_deduct = 0
    elif settle_way == '供电局划扣(南方电网)':
        gdj_deduct = total
        sys_elec = 0
        offline_transfer = 0
    elif settle_way == '手动抄表结算':
        offline_transfer = total
        sys_elec = 0
        gdj_deduct = 0
    return {
        'sys_elec_y': round(sys_elec, 2),
        'offline_transfer_y': round(offline_transfer, 2),
        'gdj_deduct_y': round(gdj_deduct, 2),
        'total_y': round(total, 2),
        'detail': {
            '自动结算(柜机上报)': {'cnt': _int_rnd(1, 12), 'fee_y': round(sys_elec, 2)},
            '手动抄表结算': {'cnt': _int_rnd(0, 6), 'fee_y': round(offline_transfer, 2)},
            '供电局划扣(南方电网)': {'cnt': _int_rnd(0, 4), 'fee_y': round(gdj_deduct, 2)},
        },
        'cnt': _int_rnd(1, 20),
    }


def _mk_share_detail(agency_name):
    share_names = ['落柜场地换电分成', '用户拓展换电分成', '激励补贴', '电费补贴', '销售补贴']
    n = _int_rnd(1, 4)
    picked = random.sample(share_names, n)
    out = []
    for name in picked:
        fee = _rnd(200, 4000)
        direction = 'out' if name != '销售补贴' else 'in'
        out.append({'expense_name': name, 'expense_type': '分成', 'expense_value': 10,
                    'expense_value_type': '按比例', 'direction': direction,
                    'cnt': _int_rnd(1, 10), 'fee_y': fee})
    return out


def build_site_value(date_from=None, date_to=None, **kw):
    if not date_from:
        date_from = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    if not date_to:
        date_to = datetime.date.today().isoformat()
    sites = _mk_sites()
    rows = []
    for s in sites:
        elec = _mk_electric_detail(s['settle_way'])
        share_detail = _mk_share_detail(s['agency_name'])
        share_fee_in = sum(x['fee_y'] for x in share_detail if x['direction'] == 'in')
        share_fee_out = sum(x['fee_y'] for x in share_detail if x['direction'] == 'out')
        order_cnt = _int_rnd(50, 1500)
        order_fee = _rnd(500, 9000)
        sales_cnt = _int_rnd(5, 120)
        sales_fee = _rnd(800, 15000)
        refund_fee = _rnd(0, 500)
        present_cnt = _int_rnd(2, 60)
        present_fee = round(present_cnt * _rnd(8, 15), 2)
        user_cnt = _int_rnd(30, 800)
        cabinet_cnt = _int_rnd(1, 4)
        battery_cnt = _int_rnd(8, 30)
        device_total = cabinet_cnt * 7000 + battery_cnt * 500
        device_monthly = round(device_total / 36.0 + cabinet_cnt * 30.0, 2)
        promoter_fee = _rnd(0, 1500)
        incentive = sum(x['fee_y'] for x in share_detail if '激励' in x['expense_name'] or '补贴' in x['expense_name'])
        income = sales_fee + order_fee
        cost = elec['total_y'] + share_fee_in + device_monthly + promoter_fee + present_fee
        profit = round(income - cost, 2)
        net_profit = round(profit, 2)
        ratio = round(income / max(cost, 0.01), 2)
        score = min(99, _int_rnd(35, 98))
        level = 'A' if score >= 80 else ('B' if score >= 60 else ('C' if score >= 40 else 'D'))
        rows.append({
            'date_from': date_from, 'date_to': date_to,
            'city': s['city'], 'area': s['area'], 'street': s['street'],
            'agency_id': s['agency_id'], 'agency_name': s['agency_name'], 'agency_level': s['agency_level'],
            'site_id': s['site_id'], 'site_name': s['site_name'], 'site_status': s['site_status'],
            'merchant_id': s['merchant_id'], 'merchant_name': s['merchant_name'],
            'maker_phone': s['maker_phone'], 'lp_phone': s['lp_phone'], 'store_manager_name': s['store_manager_name'],
            'battery_product': s['battery_product'], 'meter_type': s['meter_type'],
            'settle_way': s['settle_way'], 'settle_cycle': s['settle_cycle'],
            'electric_price_y': s['electric_price_y'],
            'electric_fee_y': elec['total_y'], 'electric_cnt': elec['cnt'],
            'sys_elec_y': elec['sys_elec_y'], 'offline_transfer_y': elec['offline_transfer_y'], 'gdj_deduct_y': elec['gdj_deduct_y'],
            'electric_detail': elec['detail'],
            'share_cnt': sum(x['cnt'] for x in share_detail),
            'share_fee_y': round(share_fee_in + share_fee_out, 2),
            'share_fee_in_y': round(share_fee_in, 2),
            'share_fee_out_y': round(share_fee_out, 2),
            'share_items': [],
            'share_detail': share_detail,
            'incentive_y': round(incentive, 2),
            'promoter_fee_y': round(promoter_fee, 2),
            'user_cnt': user_cnt,
            'device_cabinet_cnt': cabinet_cnt, 'device_battery_cnt': battery_cnt,
            'device_cost_total_y': round(device_total, 2), 'device_cost_monthly_y': device_monthly,
            'order_cnt': order_cnt, 'order_fee_y': round(order_fee, 2), 'use_power_wh': round(order_cnt * _rnd(300, 600), 0),
            'sales_cnt': sales_cnt, 'sales_fee_y': round(sales_fee, 2), 'refund_fee_y': round(refund_fee, 2),
            'present_cnt': present_cnt, 'present_fee_y': present_fee,
            'income_y': round(income, 2), 'cost_y': round(cost, 2), 'profit_y': profit,
            'profit_margin': round(profit / income * 100, 1) if income > 0 else 0,
            'net_profit_y': net_profit, 'ratio': ratio, 'score': score, 'level': level,
        })
    rows.sort(key=lambda x: -x['score'])
    return rows, date_from, date_to


def build_finance_summary(date_from=None, date_to=None):
    if not date_from:
        date_from = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    if not date_to:
        date_to = datetime.date.today().isoformat()
    data = []
    for a in AGENCIES:
        income = _rnd(20000, 200000)
        elec = _rnd(5000, 40000)
        bill_out = _rnd(10000, 80000)
        bill_in = _rnd(2000, 20000)
        sale = _rnd(10000, 90000)
        order = _rnd(8000, 70000)
        cost = elec + bill_out
        data.append({
            'agency_id': a['id'], 'agency_name': a['name'], 'agency_level': a['level'],
            'income_y': round(income, 2), 'cost_y': round(cost, 2),
            'profit_y': round(income - cost, 2),
            'ratio': round(income / max(cost, 0.01), 2),
            'sale_y': round(sale, 2), 'order_y': round(order, 2),
            'elec_y': round(elec, 2), 'bill_out_y': round(bill_out, 2), 'bill_in_y': round(bill_in, 2),
            'by_cate': [{'name': n, 'out_y': _rnd(1000, 30000), 'in_y': _rnd(100, 8000), 'cnt': _int_rnd(1, 50)} for n in EXPENSE_NAMES[:5]],
        })
    data.sort(key=lambda x: -x['cost_y'])
    return data, date_from, date_to


def build_expense_bills(agency_id=None, date_from=None, date_to=None, keyword=None,
                        page=1, page_size=50, **kw):
    if not date_from:
        date_from = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    if not date_to:
        date_to = datetime.date.today().isoformat()
    rows = []
    bid = 9000000
    for a in AGENCIES:
        if agency_id and a['id'] != agency_id:
            continue
        for i in range(_int_rnd(8, 20)):
            bid += 1
            e_name = random.choice(EXPENSE_NAMES)
            fee = _rnd(50, 5000)
            site = random.choice(['网点-%s-%d' % (a['name'], _int_rnd(1, 30))] + [m[0] for m in MERCHANTS])
            direction = 'out' if random.random() < 0.6 else 'in'
            if keyword and keyword not in e_name and keyword not in site:
                continue
            rows.append({
                'bill_id': bid,
                'bill_date': (datetime.date.today() - datetime.timedelta(days=_int_rnd(0, 90))).isoformat(),
                'settle_date': (datetime.date.today() - datetime.timedelta(days=_int_rnd(0, 90))).isoformat(),
                'business_type': random.choice(['换电分成', '电费结算', '补贴', '销售']),
                'business_id': bid + 100000,
                'expense_name': e_name,
                'expense_type': random.choice(['分成', '电费', '补贴', '提成']),
                'expense_value': random.choice([None, 5, 10, 15, 0.8]),
                'expense_value_type': random.choice(['按比例', '固定金额', '按电量', '']),
                'standard_expense_desc': '',
                'fee_y': round(fee, 2),
                'after_taxes_fee_y': round(fee * 0.97, 2),
                'in_unit': 'swapAgency' if direction == 'in' else 'swapSite',
                'in_unit_id': a['id'] if direction == 'in' else 0,
                'in_unit_name': a['name'] if direction == 'in' else site,
                'out_unit': 'swapSite' if direction == 'in' else 'swapAgency',
                'out_unit_id': 0 if direction == 'in' else a['id'],
                'out_unit_name': site if direction == 'in' else a['name'],
                'agency_id': a['id'],
                'agency_name': a['name'],
            })
    rows.sort(key=lambda x: -x['bill_id'])
    total = len(rows)
    if page_size > 0:
        rows = rows[(page - 1) * page_size: page * page_size]
    return rows, total, date_from, date_to


def build_overview(date_from=None, date_to=None):
    """总看板：KPI + 收入/支出结构 + 趋势 + 代理商排行 + 最具价值网点TOP"""
    if not date_from:
        date_from = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    if not date_to:
        date_to = datetime.date.today().isoformat()
    sites, _, _ = build_site_value(date_from, date_to)
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
        {'name': '分成/补贴收入', 'value_y': round(share_in, 2)},
    ]
    cost_breakdown = [
        {'name': '网点电费', 'value_y': kpi['elec_y']},
        {'name': '分成/补贴支出', 'value_y': share_out},
        {'name': '业务员提成', 'value_y': promo},
        {'name': '赠送电量包(客情)', 'value_y': present_v},
        {'name': '设备投入(柜+电池)', 'value_y': device_total},
    ]
    # 近90天趋势(演示按天)
    trend = []
    d0 = datetime.date.fromisoformat(date_from)
    d1 = datetime.date.fromisoformat(date_to)
    nd = max(1, (d1 - d0).days)
    step = max(1, nd // 30)
    cur = d0
    while cur <= d1:
        base = random.random()
        trend.append({'date': cur.isoformat(),
                      'income_y': round(total_income / 90 * (0.8 + base * 0.4), 2),
                      'cost_y': round(total_cost / 90 * (0.85 + base * 0.3), 2),
                      'profit_y': round(total_income / 90 * (0.8 + base * 0.4) - total_cost / 90 * (0.85 + base * 0.3), 2)})
        cur += datetime.timedelta(days=step)
    agency_top = []
    for d in fin:
        agency_top.append({'agency_id': d['agency_id'], 'agency_name': d['agency_name'],
                           'income_y': d['income_y'], 'cost_y': d['cost_y'], 'profit_y': d['profit_y'],
                           'score': random.randint(55, 95)})
    top_sites = [{'site_id': s['site_id'], 'site_name': s['site_name'], 'city': s['city'],
                  'agency_name': s['agency_name'], 'income_y': s['income_y'], 'profit_y': s['profit_y'],
                  'score': s['score'], 'level': s['level']} for s in sites[:10]]
    score_dist = {'A': sum(1 for s in sites if s['level'] == 'A'),
                  'B': sum(1 for s in sites if s['level'] == 'B'),
                  'C': sum(1 for s in sites if s['level'] == 'C'),
                  'D': sum(1 for s in sites if s['level'] == 'D')}
    return {'kpi': kpi, 'income_breakdown': income_breakdown, 'cost_breakdown': cost_breakdown,
            'trend': trend, 'agency_top': agency_top, 'top_sites': top_sites,
            'score_dist': score_dist, 'site_cnt': len(sites),
            'date_from': date_from, 'date_to': date_to}


def build_finance_detail(date_from=None, date_to=None):
    """财务看板：公司支出/网点收入/设备投入/员工提成/运营/营收/电费/分成/赠送包 成本费用结构"""
    if not date_from:
        date_from = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    if not date_to:
        date_to = datetime.date.today().isoformat()
    sites, _, _ = build_site_value(date_from, date_to)
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
    # 软硬件/运营投入按演示估算
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
        {'cate': '软硬件投入', 'name': '软硬件其他投入', 'value_y': software_y + hardware_other_y, 'kind': 'cost'},
        {'cate': '运营投入', 'name': '运营投入', 'value_y': operate_y, 'kind': 'cost'},
        {'cate': '员工成本', 'name': '业务员提成/佣金', 'value_y': promo, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '系统结算电费(自动)', 'value_y': sys_elec, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '线下结算-手动抄表(对公)', 'value_y': offline_elec, 'kind': 'cost'},
        {'cate': '网点电费', 'name': '线下结算-供电局划扣', 'value_y': gdj_elec, 'kind': 'cost'},
        {'cate': '分成支出', 'name': '公司支付给网点的分成/补贴', 'value_y': share_out, 'kind': 'cost'},
        {'cate': '客情维护', 'name': '赠送电量包(%d笔)' % present_cnt, 'value_y': present_v, 'kind': 'cost'},
    ]
    return {'summary': summary, 'items': items, 'date_from': date_from, 'date_to': date_to}


def filters():
    """离线筛选维度"""
    sites = _mk_sites()
    cities = sorted(set(s['city'] for s in sites))
    agencies = [{'id': a['id'], 'name': a['name']} for a in AGENCIES]
    products = [{'id': i + 1, 'name': p} for i, p in enumerate(BATTERY_PRODUCTS)]
    return {'agencies': agencies, 'cities': cities, 'products': products,
            'data_date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'demo': True}
