// Api Fuctions
async function postJson(url, data) {
    data['password'] = getPassword()
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    })
    return await response.json()
}

document.getElementById('pass-login').addEventListener('click', async () => {
    const password = document.getElementById('auth-pass').value
    const data = { 'pass': password }
    const json = await postJson('/api/checkPassword', data)
    if (json.status === 'ok') {
        localStorage.setItem('password', password)
        alert('Logged In Successfully')
        window.location.reload()
    }
    else {
        alert('Wrong Password')
    }

})

async function getCurrentDirectory() {
    const path = getCurrentPath()
    if (path === 'redirect') {
        return
    }
    try {
        const data = { 'path': path }
        const json = await postJson('/api/getDirectory', data)

        if (json.status === 'ok') {
            console.log(json)
            showDirectory(json['data'])
        } else {
            alert('404 Current Directory Not Found')
        }
    }
    catch (err) {
        console.log(err)
        alert('404 Current Directory Not Found')
    }
}

async function createNewFolder() {
    const folderName = document.getElementById('new-folder-name').value;
    const path = getCurrentPath()
    if (path === 'redirect') {
        return
    }
    if (folderName.length > 0) {
        const data = {
            'name': folderName,
            'path': path
        }
        try {
            const json = await postJson('/api/createNewFolder', data)

            if (json.status === 'ok') {
                window.location.reload();
            } else {
                alert(json.status)
            }
        }
        catch (err) {
            alert('Error Creating Folder')
        }
    } else {
        alert('Folder Name Cannot Be Empty')
    }
}

// File Uploader

const MAX_FILE_SIZE = MAX_FILE_SIZE__SDGJDG // Will be replaced by the python

const fileInput = document.getElementById('fileInput');
const progressBar = document.getElementById('progress-bar');
const cancelButton = document.getElementById('cancel-file-upload');
const uploadPercent = document.getElementById('upload-percent');
let uploadRequest = null;
let uploadStep = 0;
let uploadID = null;

fileInput.addEventListener('change', async (e) => {
    const file = fileInput.files[0];

    if (file.size > MAX_FILE_SIZE) {
        alert(`File size exceeds ${(MAX_FILE_SIZE / (1024 * 1024 * 1024)).toFixed(2)} GB limit`);
        return;
    }

    // Showing file uploader
    document.getElementById('bg-blur').style.zIndex = '2';
    document.getElementById('bg-blur').style.opacity = '0.1';
    document.getElementById('file-uploader').style.zIndex = '3';
    document.getElementById('file-uploader').style.opacity = '1';

    document.getElementById('upload-filename').innerText = 'Filename: ' + file.name;
    document.getElementById('upload-filesize').innerText = 'Filesize: ' + (file.size / (1024 * 1024)).toFixed(2) + ' MB';
    document.getElementById('upload-status').innerText = 'Status: Uploading To Backend Server';


    const formData = new FormData();
    formData.append('file', file);
    formData.append('path', getCurrentPath());
    formData.append('password', getPassword());
    const id = getRandomId();
    formData.append('id', id);
    formData.append('total_size', file.size);

    uploadStep = 1;
    uploadRequest = new XMLHttpRequest();
    uploadRequest.open('POST', '/api/upload', true);

    uploadRequest.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percentComplete = (e.loaded / e.total) * 100;
            progressBar.style.width = percentComplete + '%';
            uploadPercent.innerText = 'Progress : ' + percentComplete.toFixed(2) + '%';
        }
    });

    uploadRequest.upload.addEventListener('load', async () => {
        await updateSaveProgress(id)
    });

    uploadRequest.upload.addEventListener('error', () => {
        alert('Upload failed');
        window.location.reload();
    });

    uploadRequest.send(formData);
});

cancelButton.addEventListener('click', () => {
    if (uploadStep === 1) {
        uploadRequest.abort();
    } else if (uploadStep === 2) {
        const data = { 'id': uploadID }
        postJson('/api/cancelUpload', data)
    }
    alert('Upload canceled');
    window.location.reload();
});

async function updateSaveProgress(id) {
    console.log('save progress')
    progressBar.style.width = '0%';
    uploadPercent.innerText = 'Progress : 0%'
    document.getElementById('upload-status').innerText = 'Status: Processing File On Backend Server';

    const interval = setInterval(async () => {
        const response = await postJson('/api/getSaveProgress', { 'id': id })
        const data = response['data']

        if (data[0] === 'running') {
            const current = data[1];
            const total = data[2];
            document.getElementById('upload-filesize').innerText = 'Filesize: ' + (total / (1024 * 1024)).toFixed(2) + ' MB';

            const percentComplete = (current / total) * 100;
            progressBar.style.width = percentComplete + '%';
            uploadPercent.innerText = 'Progress : ' + percentComplete.toFixed(2) + '%';
        }
        else if (data[0] === 'completed') {
            clearInterval(interval);
            uploadPercent.innerText = 'Progress : 100%'
            progressBar.style.width = '100%';

            await handleUpload2(id)
        }
    }, 3000)

}

async function handleUpload2(id) {
    console.log(id)
    document.getElementById('upload-status').innerText = 'Status: Uploading To Telegram Server';
    progressBar.style.width = '0%';
    uploadPercent.innerText = 'Progress : 0%';

    const interval = setInterval(async () => {
        const response = await postJson('/api/getUploadProgress', { 'id': id })
        const data = response['data']

        if (data[0] === 'running') {
            const current = data[1];
            const total = data[2];
            document.getElementById('upload-filesize').innerText = 'Filesize: ' + (total / (1024 * 1024)).toFixed(2) + ' MB';

            let percentComplete
            if (total === 0) {
                percentComplete = 0
            }
            else {
                percentComplete = (current / total) * 100;
            }
            progressBar.style.width = percentComplete + '%';
            uploadPercent.innerText = 'Progress : ' + percentComplete.toFixed(2) + '%';
        }
        else if (data[0] === 'completed') {
            clearInterval(interval);
            alert('Upload Completed')
            window.location.reload();
        }
    }, 3000)
}