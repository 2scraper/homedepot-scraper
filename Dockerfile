# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium -- for a CI canary run or a scheduled job.
# Not required for local development, where `pip install` directly is simpler.
#
#   docker build -t homedepot-scraper .
#   docker run --rm -v "$PWD/out:/out" --env-file .env homedepot-scraper \
#     --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
#     --pages 3 --out /out/miter
#
# Credentials come in through --env-file or a mounted /app/.env. NOTHING here
# bakes one in: a .env baked into an image is a credential published to
# everyone who can pull it.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies --
    # not pip packages, so this has to be a separate, explicit step.
    && playwright install --with-deps chromium

# Every module the entrypoints import, transitively, plus diff_runs.py as a
# useful companion in the same image. smoke_test.py checks this list against
# the real import graph: this family has shipped an image that died with
# ModuleNotFoundError on every invocation, --help included, three times,
# because nothing in a repo like this ever builds the image.
COPY browser_bridge.py captcha_solver.py catalog_walk.py diff_runs.py \
     env_config.py fingerprint_client.py http_scraper.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     scraper_api_client.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
