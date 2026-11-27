ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        mergerfs \
        fuse3 \
        ntfs-3g \
        cifs-utils \
        samba \
        samba-common-bin \
        samba-vfs-modules \
        wsdd \
        procps \
        nfs-common \
        smartmontools \
        util-linux \
        e2fsprogs \
        xfsprogs \
        btrfs-progs \
        hdparm \
        kmod \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs /
RUN chmod 755 /etc/services.d/nas/run /etc/services.d/nas/finish
