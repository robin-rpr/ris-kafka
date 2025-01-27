FROM python:3.11.3

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    clang \
    libclang-dev \
    netcat

# Install Kafka
RUN apt-get install -y librdkafka-dev

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .