# Create common base stage
FROM python:3.6-slim as base

WORKDIR /build

# Create virtualenv to isolate builds
RUN python -m venv /build

# Install common libraries
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
    # required by psycopg2 at build and runtime
    libpq-dev \
     # required for health check
    curl \
 && apt-get autoremove -y

# Make sure we use the virtualenv
ENV PATH="/build/bin:$PATH"

# Stage to build and install everything
FROM base as builder

WORKDIR /src

# Install all required build libraries
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    openssh-client \
    graphviz-dev \
    pkg-config \
    git-core \
    openssl \
    libssl-dev \
    libffi6 \
    libffi-dev \
    libpng-dev

# Make sure we have the latest pip version
RUN pip install -U pip

# Download mitie model
RUN wget -P /app/data/ https://s3-eu-west-1.amazonaws.com/mitie/total_word_feature_extractor.dat

# Copy only what we really need
COPY README.md .
COPY setup.py .
COPY setup.cfg .
COPY MANIFEST.in .
COPY alt_requirements/ ./alt_requirements
COPY requirements.txt .
COPY LICENSE.txt .

# Install dependencies
RUN pip install --no-cache-dir -r alt_requirements/requirements_full.txt

# Install and link spacy models
RUN pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_md-2.1.0/en_core_web_md-2.1.0.tar.gz#egg=en_core_web_md==2.1.0 --no-cache-dir > /dev/null \
 && python -m spacy link en_core_web_md en \
 && pip install https://github.com/explosion/spacy-models/releases/download/de_core_news_sm-2.1.0/de_core_news_sm-2.1.0.tar.gz#egg=de_core_news_sm==2.1.0 --no-cache-dir > /dev/null \
 && python -m spacy link de_core_news_sm de

# Install Rasa as package
COPY rasa ./rasa
RUN pip install .[sql,spacy,mitie]

# Runtime stage which uses the virtualenv which we built in the previous stage
FROM base AS runner

WORKDIR /app

# Copy over default pipeline config
COPY sample_configs/config_pretrained_embeddings_spacy_duckling.yml config.yml

# Copy over mitie model
COPY --from=builder /app/data/total_word_feature_extractor.dat data/total_word_feature_extractor.dat

# Copy virtualenv from previous stage
COPY --from=builder /build /build

# Create a volume for temporary data
VOLUME /tmp

# Make sure the default group has the same permissions as the owner
RUN chgrp -R 0 . && chmod -R g=u .

# Don't run as root
USER 1001

EXPOSE 5005

ENTRYPOINT ["rasa"]
CMD ["--help"]