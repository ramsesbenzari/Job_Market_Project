import os
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
URL = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"

response = requests.get(URL)
data = response.json()

for model in data.get('models', []):
    print(f"Model Name: {model['name']}")