import os

import uvicorn


if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=port, reload=False)
