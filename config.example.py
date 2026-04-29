r"""
Copy this file to `config.py` (repo root) and fill in real values.
config.py is gitignored and must NEVER be committed.

On EC2, either:
  (a) manually copy config.py via RDP to C:\siel\config.py (this project's chosen approach)
  (b) load from AWS Secrets Manager / env vars (future)

Do NOT paste real credentials into this example file.
"""

# IMPORTANT: DB_CONFIG must NOT include 'database' key.
# common/base_crawler.py connect_db() passes database='postgres' explicitly,
# so including it here causes "got multiple values for keyword argument 'database'".
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'TBD',
}

AMAZON_LOGIN_1 = {'email': 'TBD', 'password': 'TBD'}
AMAZON_LOGIN_2 = {'email': 'TBD', 'password': 'TBD'}
AMAZON_LOGIN_3 = {'email': 'TBD', 'password': 'TBD'}

AMAZON_ACCOUNTS = [AMAZON_LOGIN_1, AMAZON_LOGIN_2, AMAZON_LOGIN_3]

FLIPKART_LOGIN_1 = {'email': 'TBD', 'password': 'TBD'}
FLIPKART_LOGIN_2 = {'email': 'TBD', 'password': 'TBD'}
FLIPKART_LOGIN_3 = {'email': 'TBD', 'password': 'TBD'}

FLIPKART_ACCOUNTS = [FLIPKART_LOGIN_1, FLIPKART_LOGIN_2, FLIPKART_LOGIN_3]

EMAIL_CONFIG = {
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'sender_email': 'TBD',
    'sender_password': 'TBD',
    'receiver_email': 'TBD',
}

OPENAI_API_KEY = 'TBD'
FRED_API_KEY = 'TBD'
