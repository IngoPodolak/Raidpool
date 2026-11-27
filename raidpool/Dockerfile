ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Grundpakete
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        mergerfs \
        fuse3 \
        ntfs-3g \
        cifs-utils \
        nfs-common \
        samba \
        samba-common-bin \
        samba-vfs-modules \
        procps \
        smbclient \
        iproute2 \
        iputils-arping \
        attr \
        smartmontools \
        util-linux \
        e2fsprogs \
        xfsprogs \
        btrfs-progs \
        exfatprogs \
        hdparm \
        kmod \
    && rm -rf /var/lib/apt/lists/*

# Optional: Windows-Netzwerkumgebung (wsdd)
RUN apt-get update \
    && (apt-get install -y --no-install-recommends wsdd || echo "wsdd nicht verfügbar") \
    && rm -rf /var/lib/apt/lists/*

# Optional: ntfsck (echte NTFS-Prüfung/Reparatur unter Linux, Paket ntfsprogs-plus aus trixie-backports).
# Wird getrennt unter /opt/ntfsplus abgelegt, damit es nicht mit den Werkzeugen von ntfs-3g kollidiert.
RUN echo "deb http://deb.debian.org/debian trixie-backports main" > /etc/apt/sources.list.d/backports.list \
    && apt-get update \
    && mkdir -p /tmp/ntfsplus /opt/ntfsplus \
    && cd /tmp/ntfsplus \
    && (apt-get download -t trixie-backports ntfsprogs-plus \
        && dpkg-deb -x ntfsprogs-plus_*.deb /opt/ntfsplus \
        && LIBDIR="$(dirname "$(find /opt/ntfsplus -name 'lib*.so*' | head -1)" 2>/dev/null || echo /opt/ntfsplus/usr/lib)" \
        && BIN="$(find /opt/ntfsplus -type f -name ntfsck | head -1)" \
        && printf '#!/bin/sh\nLD_LIBRARY_PATH="%s" exec "%s" "$@"\n' "$LIBDIR" "$BIN" > /usr/local/bin/ntfsck \
        && chmod 755 /usr/local/bin/ntfsck \
        && /usr/local/bin/ntfsck --help >/dev/null 2>&1 || /usr/local/bin/ntfsck -h >/dev/null 2>&1 || true \
        || echo "ntfsck nicht verfügbar – es wird ntfsfix verwendet") \
    && rm -rf /tmp/ntfsplus /etc/apt/sources.list.d/backports.list /var/lib/apt/lists/*

COPY rootfs /
RUN chmod 755 /etc/services.d/nas/run /etc/services.d/nas/finish
