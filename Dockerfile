ARG SETUPTOOLS_VERSION=80.10.2
ARG PYTHON_IMAGE=python:3.12-slim

# Build container
FROM ${PYTHON_IMAGE} AS build

ARG SETUPTOOLS_VERSION
ARG OPERATOR_VERSION

RUN mkdir -pv /src

WORKDIR /src

RUN apt-get update && \
    apt-get install git -y

COPY . /src
RUN python -m pip install -U setuptools==${SETUPTOOLS_VERSION} \
    && pip install setuptools_scm vcs_versioning \
    && if [ -n "${OPERATOR_VERSION}" ]; then \
         export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CRATE_OPERATOR="${OPERATOR_VERSION}"; \
       fi \
    && python setup.py clean bdist_wheel


# Run container
FROM ${PYTHON_IMAGE}

ARG PYTHON_IMAGE
ARG OPERATOR_VERSION
ARG SOURCE_URL=https://github.com/crate/crate-operator
ARG SOURCE_REVISION
ARG SOURCE_STATE
ARG SOURCE_PATCH_SHA256

LABEL license="Apache License 2.0" \
    maintainer="Crate.IO GmbH <office@crate.io>" \
    name="CrateDB Kubernetes Operator" \
    repository="crate/crate-operator" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.source="${SOURCE_URL}" \
    org.opencontainers.image.revision="${SOURCE_REVISION}" \
    org.opencontainers.image.version="${OPERATOR_VERSION}" \
    org.opencontainers.image.base.name="${PYTHON_IMAGE}" \
    io.hive.source.state="${SOURCE_STATE}" \
    io.hive.source.patch-sha256="${SOURCE_PATCH_SHA256}"

ARG SETUPTOOLS_VERSION

WORKDIR /etc/cloud
RUN useradd -U -M crate-operator

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /src/dist /wheels
COPY --from=build /src/LICENSE /usr/share/licenses/crate-operator/LICENSE

RUN pip install --no-cache-dir -U pip wheel setuptools==${SETUPTOOLS_VERSION} && \
    pip install --no-cache-dir /wheels/*.whl && \
    rm -rf /wheels && \
    rm -f /usr/local/lib/python3.12/site-packages/pip/_vendor/bom.cdx.json && \
    ln -s "$(python -c "import pkgutil; main = pkgutil.get_loader('crate.operator.main'); print(main.path)")"

USER crate-operator

ENTRYPOINT ["kopf", "run", "--standalone", "-A"]
CMD ["main.py"]
