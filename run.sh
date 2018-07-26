HOST_ADDR=`python -c "import socket;print(socket.getfqdn())"`
docker run -d --runtime=nvidia -p 9999:9999 -e HOST_ADDR=$(HOST_ADDR) --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock --label ai.scalabel.app=orchestrator simonmok/scalabel-orch
