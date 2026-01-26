FROM python:3.12-alpine3.20

RUN apk add --no-cache \
    build-base \
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

COPY app/ /app/app/
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
