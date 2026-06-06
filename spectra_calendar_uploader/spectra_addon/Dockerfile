FROM python:3.12-alpine3.20

# Installation der Abhängigkeiten mit temporärem Build-Layer
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    && apk add --no-cache \
    libjpeg-turbo-dev \
    zlib-dev \
    freetype-dev \
    lcms2-dev \
    openjpeg-dev \
    tiff-dev \
    libwebp-dev \
    font-dejavu \
    tzdata

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Build-Dependencies wieder entfernen
RUN apk del .build-deps

COPY app/ /app/app/
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]