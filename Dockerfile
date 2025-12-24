
# --- Stage 1: The Builder ---
# This stage downloads the card data and builds the SQLite database.
FROM python:3.11-slim AS builder

# Set the working directory
WORKDIR /usr/src/app

# Install OS-level dependencies if needed (e.g., for certain Python packages)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc

# Copy the requirements file and install dependencies
# This includes 'requests' needed by the build script.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code, specifically the build script
# and any modules it might depend on.
# We copy the whole 'app' directory in case of shared utilities in the future.
COPY ./app ./app
COPY ./scripts ./scripts

# Run the database build script. This will create app/data/cards.db.
# If this script fails, the docker build will stop.
RUN python scripts/build_db.py


# --- Stage 2: The Final Application ---
# This stage builds the final, lightweight image for production.
FROM python:3.11-slim

# Set the working directory
WORKDIR /usr/src/app

# Set environment variables
# Prevents Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE 1
# Ensures Python output is sent straight to the terminal without buffering
ENV PYTHONUNBUFFERED 1

# Copy the requirements file and install only production dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code from the current context
COPY ./app ./app

# Copy the pre-built database from the 'builder' stage
# This is the "baked-in" database strategy.
COPY --from=builder /usr/src/app/app/data/cards.db ./app/data/cards.db

# Expose the port the app will run on.
# Cloud Run automatically uses the PORT environment variable, defaulting to 8080.
# This EXPOSE is more for documentation and local testing.
EXPOSE 8080

# Command to run the application using Uvicorn.
# It will listen on all available network interfaces (0.0.0.0).
# The port is determined by the $PORT environment variable, which is set by Cloud Run.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
