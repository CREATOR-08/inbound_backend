
from dotenv import load_dotenv

# Load environment variables BEFORE importing the FastAPI application.
load_dotenv(override=True)

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
