"""Quick PayPal sandbox test: get a token, create an order, then capture it after you approve."""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api-m.sandbox.paypal.com"
CID = os.environ["PAYPAL_CLIENT_ID"]
SECRET = os.environ["PAYPAL_CLIENT_SECRET"]


def get_token():
    r = requests.post(f"{BASE}/v1/oauth2/token", auth=(CID, SECRET),
                      data={"grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def create_order(token, value="10.00", currency="GBP"):
    r = requests.post(f"{BASE}/v2/checkout/orders",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"intent": "CAPTURE",
                            "purchase_units": [{"amount": {"currency_code": currency, "value": value}}]})
    r.raise_for_status()
    return r.json()


def capture_order(token, order_id):
    r = requests.post(f"{BASE}/v2/checkout/orders/{order_id}/capture",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    token = get_token()
    if len(sys.argv) > 1:  # python paypal_test.py <ORDER_ID>  -> capture
        print(capture_order(token, sys.argv[1])["status"])
    else:
        order = create_order(token)
        approve = next(l["href"] for l in order["links"] if l["rel"] == "approve")
        print("Order ID:", order["id"])
        print("Approve with your sandbox PERSONAL account:", approve)
        print(f"Then run: python paypal_test.py {order['id']}")
