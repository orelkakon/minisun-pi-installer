from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from installer import run_installation

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


class InstallParams(BaseModel):
    ip: str
    port: int
    user: str
    password: str
    bid: str


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html") as f:
        return f.read()


@app.post("/install")
async def install(params: InstallParams):
    async def stream():
        async for event in run_installation(
            params.ip, params.port, params.user, params.password, params.bid
        ):
            yield event

    return EventSourceResponse(stream())
