# -*- coding: utf-8 -*-
"""网点价值与财务收支看板 - Flask 入口 (端口 8093)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\Marvis K\janus\electric")

from flask import Flask, send_from_directory
from bvapi.routes import bp as api_bp

app = Flask(__name__, static_folder='web', static_url_path='')
app.register_blueprint(api_bp, url_prefix='/api')

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

@app.route('/')
def index():
    return send_from_directory(WEB_DIR, 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(WEB_DIR, path)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8093, debug=False, threaded=True)
