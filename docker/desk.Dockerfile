FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements/desk.txt requirements/desk.txt
RUN pip install --no-cache-dir -r requirements/desk.txt

# The dashboard needs only its own code: it reaches agents and data through the backend API.
COPY app.py ./
COPY desk ./desk
COPY .streamlit ./.streamlit
COPY evaluation/scenarios.json ./evaluation/scenarios.json

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/dashboard/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
