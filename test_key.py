import os
from google import genai
from dotenv import load_dotenv
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
r = client.models.generate_content(model="gemini-3.1-flash-lite", contents="Say OK")
print(r.text)