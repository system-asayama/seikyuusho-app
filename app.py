from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>🐳 seikyuusho-app</h1><p>Compose: web + db</p>"
