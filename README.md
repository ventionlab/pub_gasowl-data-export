# GasOwl data export

A small Python program that reads your GasOwl gas levels and writes them to a CSV file, which opens
in Excel, Numbers or Google Sheets: your current levels, hourly history, or daily use.

**To set it up, follow the guide "Your gas levels in a CSV file or Google Sheets"** in the GasOwl
app, under Guides (sign in at [app.gasowl.co.nz](https://app.gasowl.co.nz) or in the GasOwl app on
your phone).

## Download

Open `gasowl_export.py` above and choose **Download raw file**, or choose **Code › Download ZIP**.

## In short

```
python -m pip install boto3 requests
```

Give it your GasOwl app login in the terminal (Mac or Linux shown; the guide has PowerShell):

```
export GASOWL_USERNAME="you@example.com"
export GASOWL_PASSWORD="your GasOwl password"
```

Then, always with `--role export`, which lets the program read and never change anything:

```
python gasowl_export.py --role export
python gasowl_export.py --role export --output levels.csv --append
python gasowl_export.py --role export --mode history --days 30 --output history.csv
python gasowl_export.py --role export --mode daily --days 90 --output daily.csv
```

## Help

support@ventionlab.tech · New Zealand 0800 427 695 · Australia +61 2 4012 2479 · USA +1 (940) 289-8972

---

GasOwl by [Vention Lab](https://www.ventionlab.tech) · © Vention Lab 2026
