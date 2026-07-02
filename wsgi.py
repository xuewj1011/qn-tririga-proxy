# PythonAnywhere WSGI configuration
# Place this file in /var/www/ on PythonAnywhere and point your web app to it

import sys
import os

# Project path — adjust to your PythonAnywhere username
project_path = '/home/xuewj1011/qn-tririga-proxy'
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# Set environment variables (or configure in PythonAnywhere dashboard)
os.environ.setdefault('FEISHU_APP_ID', 'cli_aaca763cbe399bfb')
os.environ.setdefault('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
# IMPORTANT: Set FEISHU_APP_SECRET and DEEPSEEK_API_KEY in the PythonAnywhere
# "Environment variables" section or replace with actual values here.

from app import app as application
