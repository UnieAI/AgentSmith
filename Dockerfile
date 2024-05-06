FROM swr.cn-north-4.myhuaweicloud.com/infiniflow/ragflow-base:v1.0
USER  root

WORKDIR /ragflow

ADD ./web ./web
RUN cd ./web && npm i && npm run build

ADD ./api ./api
ADD ./conf ./conf
ADD ./deepdoc ./deepdoc
ADD ./rag ./rag

ENV PYTHONPATH=/ragflow/
ENV HF_ENDPOINT=https://hf-mirror.com

ADD docker/entrypoint.sh ./entrypoint.sh

ADD requirements.txt requirements.txt

RUN pip install -r requirements.txt

RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
