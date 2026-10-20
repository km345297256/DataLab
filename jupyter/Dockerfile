FROM docker.1ms.run/python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JUPYTER_ENABLE_LAB=yes

RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' \
    /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' 'Acquire::Retries "5";' 'Acquire::http::Timeout "60";' > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential gdal-bin libgdal-dev libproj-dev proj-data proj-bin \
    libgeos-dev libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ \
    jupyterlab==4.* ipykernel notebook \
    numpy pandas scipy matplotlib openpyxl odfpy \
    xarray netCDF4 h5netcdf rasterio geopandas shapely pyproj \
    python-docx pypdf pymupdf

RUN useradd --create-home --uid 1000 --shell /bin/bash jovyan && \
    mkdir -p /home/jovyan/work /home/jovyan/datasets && \
    chown -R jovyan:jovyan /home/jovyan

USER jovyan
WORKDIR /home/jovyan/work
EXPOSE 8888

ENTRYPOINT ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--ServerApp.root_dir=/home/jovyan/work", "--ServerApp.allow_remote_access=True"]
