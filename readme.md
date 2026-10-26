# PDF Crusher

A local-first PDF compression tool built with Python and Flask.

PDF Crusher accepts PDF uploads and attempts to reduce their file size while keeping the document readable. The web interface provides a simple upload, compression, and download workflow.

## Features

- Browser-based PDF upload and download.
- Automatic PDF compression.
- Configurable target file size, currently targeting approximately 499 KB.
- OpenCV-based image processing as part of the compression pipeline.
- Isolated Python dependencies through a project-local virtual environment.
- Windows batch scripts for installation and startup.

Compression results depend on the input document. Readability and output size are not guaranteed for every PDF.

## Requirements

- Windows
- Python 3.12
- Internet access during initial dependency installation

## Setup

From the repository root, run:

```bat
web\install.bat
```

This creates the `.venv` environment and installs the dependencies listed in `web/requirements.txt`.

To launch the application:

```bat
web\run.bat
```

The application uses port `18000` by default, unless overridden by the `PORT` environment variable.

Open `http://127.0.0.1:18000` in your browser.

## Project Structure

```text
pdfcrusher/
├── pdfcrush.py
├── .gitignore
├── README.md
├── .venv/                  # Generated locally; not committed
└── web/
    ├── app.py
    ├── install.bat
    ├── requirements.txt
    ├── run.bat
    ├── static/
    │   └── index.html
    └── jobs/               # Runtime data; not committed
```

## Design Decisions

### Keep compression separate from document cleanup

The current compression engine focuses on reducing PDF file size. It should remain stable and independently usable.

Photographed documents can contain backgrounds, shadows, perspective distortion, and excess space around the page. Future document cleanup functionality may detect page boundaries, crop backgrounds, correct perspective, and enhance readability.

That functionality should be implemented as an optional preprocessing stage rather than silently changing the behavior of the existing compression engine.

### Keep the Python environment isolated

Dependencies are installed into `.venv`. The virtual environment itself is not committed; `web/requirements.txt` defines the dependencies needed to recreate it.

### Exclude runtime and personal data

Uploaded files, generated PDFs, job status files, logs, and local environment configuration are excluded from version control.

## Security Notes

This project was initially designed for local use. Before exposing it to other users, review authentication, upload limits, file handling, job cleanup, and access controls.

If deployed through a public hostname, protect it with an appropriate access-control layer. Do not assume that a Cloudflare Tunnel alone provides application-level authentication.

## Current Scope

The current version focuses on PDF compression. CamScanner-style page detection and cropping are possible future features and are not assumed to be part of the existing compression workflow.