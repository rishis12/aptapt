from app import create_app

app = create_app()

if __name__ == "__main__":
    # Port 5000 is taken by AirPlay Receiver on macOS.
    app.run(debug=True, port=5050)
