FROM nginx:1.27.5-alpine

COPY file-proxy.conf /etc/nginx/nginx.conf

USER 10001:10001

ENTRYPOINT ["nginx", "-g", "daemon off;"]
