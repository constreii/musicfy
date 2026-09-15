import ngrok

def connect_ngrok():
    forwarder = ngrok.forward("localhost:5000", authtoken_from_env=True, domain="tarmac-churn-sappiness.ngrok-free.dev")
    print(f"Available at: {forwarder.url()}")

connect_ngrok()
