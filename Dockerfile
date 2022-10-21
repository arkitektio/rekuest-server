FROM python:3.8
LABEL maintainer="jhnnsrs@gmail.com"


# Install Minimal Dependencies for Django
RUN pip install poetry


# Install Arbeid
RUN mkdir /workspace
ADD . /workspace
WORKDIR /workspace

RUN poetry config virtualenvs.create false 
RUN poetry install


CMD bash run.sh


