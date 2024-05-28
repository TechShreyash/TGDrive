import shutil, asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from config import ADMIN_PASSWORD, MAX_FILE_SIZE, STORAGE_CHANNEL
from utils.clients import initialize_clients
from utils.directoryHandler import (
    backup_drive_data,
    getRandomID,
    loadDriveData,
)
from utils.extra import convert_class_to_dict
from utils.streamer import media_streamer
from utils.uploader import STOP_TRANSMISSION, PROGRESS_CACHE, start_file_uploader
from utils.logger import Logger

app = FastAPI(docs_url=None, redoc_url=None)
logger = Logger("main")


@app.get("/")
async def home_page():
    return FileResponse("website/home.html")


@app.get("/static/{file_path:path}")
async def static_files(file_path):
    return FileResponse(f"website/static/{file_path}")


@app.get("/file")
async def dl_file(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    path = request.query_params["path"]
    file = DRIVE_DATA.get_file(path)
    return await media_streamer(STORAGE_CHANNEL, file.file_id, file.name, request)


# Api Routes


@app.post("/api/checkPassword")
async def check_password(request: Request):
    data = await request.json()
    if data["pass"] == ADMIN_PASSWORD:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "Invalid password"})


@app.post("/api/createNewFolder")
async def api_new_folder(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("createNewFolder", data)
    folder_data = DRIVE_DATA.get_directory(data["path"]).contents
    for id in folder_data:
        f = folder_data[id]
        if f.type == "folder":
            if f.name == data["name"]:
                return JSONResponse(
                    {
                        "status": "Folder with the name already exist in current directory"
                    }
                )

    DRIVE_DATA.new_folder(data["path"], data["name"])
    return JSONResponse({"status": "ok"})


@app.post("/api/getDirectory")
async def api_get_directory(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("getFolder", data)

    if data["path"] == "/trash":
        data = {"contents": DRIVE_DATA.get_trashed_files_folders()}
        folder_data = convert_class_to_dict(data, showtrash=True)

    else:
        folder_data = DRIVE_DATA.get_directory(data["path"])
        folder_data = convert_class_to_dict(folder_data, showtrash=False)
    print(folder_data)
    return JSONResponse({"status": "ok", "data": folder_data})


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...), path: str = Form(...), password: str = Form(...)
):
    if password != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    # Check the file size
    file_size = 0
    for chunk in file.file:
        file_size += len(chunk)
        if file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File size exceeds 2GB limit")

    # Reset file pointer to the beginning
    file.file.seek(0)

    id = getRandomID()
    ext = file.filename.split(".")[-1]
    file_location = f"./cache/{id}.{ext}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    asyncio.create_task(start_file_uploader(file_location, id, path, file.filename))

    return JSONResponse({"id": id, "status": "ok"})


@app.post("/api/getUploadProgress")
async def get_upload_progress(request: Request):
    global PROGRESS_CACHE
    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("getUploadProgress", data)
    progress = PROGRESS_CACHE.get(data["id"], ("running", 0, 0))
    return JSONResponse({"status": "ok", "data": progress})


@app.post("/api/cancelUpload")
async def cancel_upload(request: Request):
    global STOP_TRANSMISSION
    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("cancelUpload", data)
    STOP_TRANSMISSION.append(data["id"])
    return JSONResponse({"status": "ok"})


@app.post("/api/renameFileFolder")
async def rename_file_folder(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("renameFileFolder", data)
    DRIVE_DATA.rename_file_folder(data["path"], data["name"])
    return JSONResponse({"status": "ok"})


@app.post("/api/trashFileFolder")
async def trash_file_folder(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("trashFileFolder", data)
    DRIVE_DATA.trash_file_folder(data["path"], data["trash"])
    return JSONResponse({"status": "ok"})


@app.post("/api/deleteFileFolder")
async def delete_file_folder(request: Request):
    from utils.directoryHandler import DRIVE_DATA

    data = await request.json()

    if data["password"] != ADMIN_PASSWORD:
        return JSONResponse({"status": "Invalid password"})

    print("deleteFileFolder", data)
    DRIVE_DATA.delete_file_folder(data["path"])
    return JSONResponse({"status": "ok"})


# Startup Event


@app.on_event("startup")
async def startup_event():
    # Create cache directory
    Path("./cache").mkdir(exist_ok=True)

    # Initialize the clients
    await initialize_clients()

    # Load the drive data
    await loadDriveData()

    # Start the backup drive data task
    asyncio.create_task(backup_drive_data())
