// Handling New Button On Sidebar Click
const isTrash = getCurrentPath().startsWith('/trash')
const isSearch = getCurrentPath().startsWith('/search')
const isShare = getCurrentPath().startsWith('/share')

if (!isTrash && !isSearch) {
    document.getElementById('new-button').addEventListener('click', () => {
        document.getElementById('new-upload').style.zIndex = '1'
        document.getElementById('new-upload').style.opacity = '1'
        document.getElementById('new-upload').style.top = '80px'
        document.getElementById('new-upload-focus').focus()
    });
}
else {
    document.getElementById('new-button').style.display = 'none'
}

if (isShare) {
    document.getElementById('new-button').style.display = 'none'
    const sections = document.querySelector('.sidebar-menu').getElementsByTagName('a')
    sections[1].remove()
}

// New File Upload Start

function closeNewUploadFocus() {
    setTimeout(() => {
        document.getElementById('new-upload').style.opacity = '0'
        document.getElementById('new-upload').style.top = '40px'
        setTimeout(() => {
            document.getElementById('new-upload').style.zIndex = '-1'
        }, 300)
    }, 200)
}
document.getElementById('new-upload-focus').addEventListener('blur', closeNewUploadFocus);
document.getElementById('new-upload-focus').addEventListener('focusout', closeNewUploadFocus);

document.getElementById('file-upload-btn').addEventListener('click', () => {
    document.getElementById('fileInput').click()
});

// New File Upload End

// New Folder Start

document.getElementById('new-folder-btn').addEventListener('click', () => {
    document.getElementById('new-folder-name').value = '';
    document.getElementById('bg-blur').style.zIndex = '2';
    document.getElementById('bg-blur').style.opacity = '0.1';

    document.getElementById('create-new-folder').style.zIndex = '3';
    document.getElementById('create-new-folder').style.opacity = '1';
    setTimeout(() => {
        document.getElementById('new-folder-name').focus();
    }, 300)
})

document.getElementById('new-folder-cancel').addEventListener('click', () => {
    document.getElementById('new-folder-name').value = '';
    document.getElementById('bg-blur').style.opacity = '0';
    setTimeout(() => {
        document.getElementById('bg-blur').style.zIndex = '-1';
    }, 300)
    document.getElementById('create-new-folder').style.opacity = '0';
    setTimeout(() => {
        document.getElementById('create-new-folder').style.zIndex = '-1';
    }, 300)
});

document.getElementById('new-folder-create').addEventListener('click', createNewFolder);

// New Folder End

// New Url Upload Start

document.getElementById('url-upload-btn').addEventListener('click', () => {
    document.getElementById('remote-url').value = '';
    document.getElementById('bg-blur').style.zIndex = '2';
    document.getElementById('bg-blur').style.opacity = '0.1';

    document.getElementById('new-url-upload').style.zIndex = '3';
    document.getElementById('new-url-upload').style.opacity = '1';
    setTimeout(() => {
        document.getElementById('remote-url').focus();
    }, 300)
})

document.getElementById('remote-cancel').addEventListener('click', () => {
    document.getElementById('remote-url').value = '';
    document.getElementById('bg-blur').style.opacity = '0';
    setTimeout(() => {
        document.getElementById('bg-blur').style.zIndex = '-1';
    }, 300)
    document.getElementById('new-url-upload').style.opacity = '0';
    setTimeout(() => {
        document.getElementById('new-url-upload').style.zIndex = '-1';
    }, 300)
});

document.getElementById('remote-start').addEventListener('click', Start_URL_Upload);

// New Url Upload End