# Free Motion's image (2026-11-10): one environment, two roles (entrypoint.sh).
#   controller  every script and every driver CLI, run by `scheduled.mjs daemon` (k8s/apps/fm-controller)
#   browser     Camoufox behind @playwright/mcp for one lane, reached over HTTP (k8s/apps/fm-browser)
# One image for both, so the controller's local Camoufox (HelloWork, APEC, questionnaires) and the lanes' browsers
# are the same build. The code is not in the image: the fm-* repos are a checkout on the server (/fm/ws).
# Build on the server: docker build -t fm:<date> .
# What the runtime user (node) writes to (Camoufox's cache, agy, which updates itself) is created owned by it and
# filled as it: a `chown -R` afterwards would copy those gigabytes into another layer.
FROM node:24.11.1-bookworm

ARG CODEX_VERSION=0.159.3
ARG COPILOT_VERSION=1.0.105
ARG CLAUDE_VERSION=2.1.317
ARG PLAYWRIGHT_VERSION=1.64.0
ARG PLAYWRIGHT_MCP_VERSION=0.0.82
# The browser the disguise was made for (Firefox 152 in its user agent), as on the PC; `camoufox fetch` alone gets the newest.
ARG CAMOUFOX_BROWSER=152.0.4-beta.31

ENV TZ=Europe/Paris \
    LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    XDG_CACHE_HOME=/opt/cache \
    npm_config_cache=/opt/npm-cache \
    npm_config_update_notifier=false \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    PLAYWRIGHT_MCP_VERSION=${PLAYWRIGHT_MCP_VERSION} \
    PATH=/opt/py/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
      tini tzdata xvfb xauth procps psmisc lsof git ca-certificates curl gnupg jq \
      python3 python3-pip \
      dbus dbus-x11 gnome-keyring libsecret-1-0 libsecret-tools \
      fonts-liberation fonts-noto-core fonts-noto-color-emoji \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# The runtime user is node (uid 1000, the owner of /srv/apps/freemotion on the server); its home is a volume.
RUN usermod -d /fm/home node \
 && mkdir -p /fm/home /fm/ws /fm/fm-data /profile /usr/local/lib/fm /opt/cache /opt/npm-cache /opt/agy/bin \
 && chown node:node /fm /fm/home /fm/ws /fm/fm-data /profile /opt/cache /opt/npm-cache /opt/agy /opt/agy/bin

# Python 3.13, as on the PC (RenderCV 2.8 needs 3.12 or newer; Debian 12 has 3.11), through uv's standalone builds:
# RenderCV (Typst comes with it), Camoufox, the Gemini website client, Playwright.
RUN python3 -m pip install --no-cache-dir --break-system-packages "uv==0.6.14" \
 && UV_PYTHON_INSTALL_DIR=/opt/python uv venv --python 3.13 /opt/py \
 && UV_PYTHON_INSTALL_DIR=/opt/python uv pip install --no-cache --python /opt/py/bin/python \
      "rendercv[full]==2.8" "camoufox==0.5.6" "gemini-webapi==2.1.1" "playwright==1.62.0" \
 && ln -sf /opt/py/bin/python /usr/local/bin/python

# Node: the driver CLIs and the browser server, pinned; Chromium for Playwright, and Firefox's libraries for Camoufox.
RUN npm install -g "@openai/codex@${CODEX_VERSION}" "@github/copilot@${COPILOT_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" "playwright@${PLAYWRIGHT_VERSION}" \
 && npm cache clean --force \
 && playwright install --with-deps chromium \
 && playwright install-deps firefox \
 && rm -rf /var/lib/apt/lists/* /tmp/*

COPY --chmod=0755 entrypoint.sh camoufox-config.mjs /usr/local/lib/fm/

USER node
# Camoufox's browser, into its cache (/opt/cache/camoufox); agy, into /opt/agy/bin (it updates itself there).
RUN python -m camoufox sync \
 && python -m camoufox fetch "official/${CAMOUFOX_BROWSER}" \
 && python -m camoufox path \
 && curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh \
 && HOME=/tmp bash /tmp/agy-install.sh -d /opt/agy/bin \
 && rm -f /tmp/agy-install.sh /tmp/.bashrc /tmp/.profile \
 && /opt/agy/bin/agy --version
USER root
RUN ln -sf /opt/agy/bin/agy /usr/local/bin/agy
USER node
WORKDIR /fm/fm-data
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/lib/fm/entrypoint.sh"]
CMD ["controller"]
