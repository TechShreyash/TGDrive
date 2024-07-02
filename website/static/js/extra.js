function getCurrentPath() {
    const url = new URL(window.location.href);
    const path = url.searchParams.get('path')
    if (path === null) {
        window.location.href = '/?path=/'
        return 'redirect'
    }
    return path
}

function getFolderAuthFromPath() {
    const url = new URL(window.location.href);
    const auth = url.searchParams.get('auth')
    return auth
}

// Changing sidebar section class
if (getCurrentPath() !== '/') {
    const sections = document.querySelector('.sidebar-menu').getElementsByTagName('a')
    sections[0].setAttribute('class', 'unselected-item')

    if (getCurrentPath().includes('/trash')) {
        sections[1].setAttribute('class', 'selected-item')
    }
}

function convertBytes(bytes) {
    const kilobyte = 1024;
    const megabyte = kilobyte * 1024;
    const gigabyte = megabyte * 1024;

    if (bytes >= gigabyte) {
        return (bytes / gigabyte).toFixed(2) + ' GB';
    } else if (bytes >= megabyte) {
        return (bytes / megabyte).toFixed(2) + ' MB';
    } else if (bytes >= kilobyte) {
        return (bytes / kilobyte).toFixed(2) + ' KB';
    } else {
        return bytes + ' bytes';
    }
}

const INPUTS = {}

function validateInput(event) {
    console.log('Validating Input')
    const pattern = /^[a-zA-Z0-9 \-_\\[\]()@#!$%*+={}:;<>,.?/|\\~`]*$/;;
    const input = event.target;
    if (!pattern.test(input.value)) {
        input.value = INPUTS[input.id]
    } else {
        INPUTS[input.id] = input.value
    }
}

function getRootUrl() {
    const url = new URL(window.location.href);
    const protocol = url.protocol; // Get the protocol, e.g., "https:"
    const hostname = url.hostname; // Get the hostname, e.g., "sub.example.com" or "192.168.1.1"
    const port = url.port; // Get the port, e.g., "8080"

    const rootUrl = `${protocol}//${hostname}${port ? ':' + port : ''}`;

    return rootUrl;
}

function copyTextToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
            alert('Link copied to clipboard!');
        }).catch(function (err) {
            console.error('Could not copy text: ', err);
            fallbackCopyTextToClipboard(text);
        });
    } else {
        fallbackCopyTextToClipboard(text);
    }
}

function fallbackCopyTextToClipboard(text) {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();

    try {
        const successful = document.execCommand('copy');
        if (successful) {
            alert('Link copied to clipboard!');
        } else {
            alert('Failed to copy the link.');
        }
    } catch (err) {
        console.error('Fallback: Oops, unable to copy', err);
    }

    document.body.removeChild(textArea);
}

function getPassword() {
    return localStorage.getItem('password')
}

function getRandomId() {
    const length = 6;
    const characters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    let result = '';
    for (let i = 0; i < length; i++) {
        result += characters.charAt(Math.floor(Math.random() * characters.length));
    }
    return result;
}

function removeSlash(text) {
    let charactersToRemove = "[/]+"; // Define the characters to remove inside square brackets
    let trimmedStr = text.replace(new RegExp(`^${charactersToRemove}|${charactersToRemove}$`, 'g'), '');
    return trimmedStr;
}