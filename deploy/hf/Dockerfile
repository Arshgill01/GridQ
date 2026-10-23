# GridQ on Hugging Face Spaces: the full app (plant twin, forecaster, LP/MPC brain, dashboard) with the
# mock MCU Bridge, since there's no UNO Q in the cloud. Built by tools/deploy_hf.sh.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 GRIDQ_MOCK_BRIDGE=1 GRIDQ_PORT=7860
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
EXPOSE 7860
CMD ["python", "-m", "gridq.io.app"]
