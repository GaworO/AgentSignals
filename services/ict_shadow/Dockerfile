FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY services/ict_shadow/requirements.txt /tmp/shadow-requirements.txt
RUN pip install --no-cache-dir -r /tmp/shadow-requirements.txt
# This build context contains ONLY the audited read-only module closure.
COPY code-manifest.json /app/code-manifest.json
COPY services/ict_shadow /app/services/ict_shadow
COPY services/tanja/ict_agent /app/services/tanja/ict_agent
COPY tools/observe_railway_shadow.py tools/run_passive_shadow.py /app/tools/
# Dedicated volume /data/shadow; never mount /data/builder50, broker data or secrets.
CMD ["sh","-c","exec gunicorn --config services/ict_shadow/gunicorn_conf.py --bind [::]:${PORT:-8090} services.ict_shadow.wsgi:app"]
