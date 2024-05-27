docker stop tgdrive
docker build -t tgdrive .
docker run -d --name tgdrive -p 80:80 tgdrive