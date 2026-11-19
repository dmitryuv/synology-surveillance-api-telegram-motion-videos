#!/bin/bash

rm -f syno_telegram_videos.tar

docker buildx build --platform linux/amd64 -t syno/syno_telegram_videos .

docker save syno/syno_telegram_videos >syno_telegram_videos.tar
