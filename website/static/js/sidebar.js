// Handling New Button On Sidebar Click
const isTrash = getCurrentPath().startsWith('/trash')
const isSearch = getCurrentPath().startsWith('/search')

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