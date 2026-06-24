import os, sys
sys.path.insert(0, r'C:\Users\wh187\Desktop\speakai')
os.chdir(r'C:\Users\wh187\Desktop\speakai')
from dotenv import load_dotenv
load_dotenv()

import websocket
from urllib.parse import urlencode

api_key = os.getenv('DEEPGRAM_API_KEY')
print('API Key present:', bool(api_key))
print('Key prefix:', api_key[:12] if api_key else 'NONE')

params = {
    'model': os.getenv('DEEPGRAM_MODEL', 'nova-2'),
    'language': os.getenv('DEEPGRAM_LANGUAGE', 'en'),
    'smart_format': 'true',
    'punctuate': 'true',
    'interim_results': 'true',
}
url = 'wss://api.deepgram.com/v1/listen?' + urlencode(params)
print('Connecting to:', url)
try:
    ws = websocket.create_connection(url, header=[f'Authorization: Token {api_key}'], timeout=10)
    print('Connection SUCCESS')
    ws.close()
except Exception as e:
    print('Connection FAILED:', type(e).__name__, e)
