import asyncio

import paramiko
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
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


class CheckParams(BaseModel):
    ip: str
    port: int
    user: str
    password: str


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html") as f:
        return f.read()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/check-connection")
async def check_connection(params: CheckParams):
    loop = asyncio.get_event_loop()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        await loop.run_in_executor(
            None,
            lambda: ssh.connect(
                params.ip, port=params.port, username=params.user,
                password=params.password, timeout=10
            ),
        )
        ssh.close()
        return JSONResponse({"status": "ok", "message": f"Connected to {params.ip}:{params.port} as {params.user}"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})


@app.post("/install")
async def install(params: InstallParams):
    async def stream():
        async for event in run_installation(
            params.ip, params.port, params.user, params.password, params.bid
        ):
            yield event

    return EventSourceResponse(stream())
