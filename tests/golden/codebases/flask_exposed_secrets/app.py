"""Flask app with multiple security issues."""

from flask import Flask, render_template, session, request, jsonify

# Hardcoded secret key
SECRET_KEY = "supersecret123"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# No session cookie security flags
# app.config["SESSION_COOKIE_SECURE"] = False (default)
# app.config["SESSION_COOKIE_HTTPONLY"] = False (default)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def get_data():
    return jsonify({"message": "Hello", "user": session.get("user", "anonymous")})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    # No input validation
    session["user"] = data["username"]
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # Debug mode in production — exposes Werkzeug debugger
    app.run(debug=True, host="0.0.0.0", port=5000)
