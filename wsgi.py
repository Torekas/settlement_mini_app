from app import app

application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Listen on all interfaces so Render’s proxy can reach it
    application.run(host="0.0.0.0", port=port)
