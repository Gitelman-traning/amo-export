# Хром + Playwright
FROM apify/actor-node-playwright-chrome:20

# будем работать в этом каталоге
WORKDIR /usr/src/app

# временно переключимся на root, чтобы ставить зависимости
USER root

# сначала только package*.json — быстрее кэшируется
COPY package*.json ./
RUN npm install --omit=dev --no-audit --no-fund

# теперь весь код
COPY . ./

# отдаём права пользователю myuser для рантайма
RUN chown -R myuser:myuser /usr/src/app

# возвращаемся к стандартному пользователю
USER myuser

# старт
CMD ["npm", "start", "--silent"]