from fastapi import FastAPI

app = FastAPI(title="ledger-api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
