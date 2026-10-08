FROM node:24-slim
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps ./apps
RUN npm ci
ARG APP=dashboard
ENV APP_NAME=$APP
RUN npm run build --workspace=@adaptivenet/$APP
USER node
CMD ["sh", "-c", "npm run start --workspace=@adaptivenet/$APP_NAME"]
