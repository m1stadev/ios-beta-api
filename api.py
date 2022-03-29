import uvicorn

if __name__ == '__main__':
    uvicorn.run(app='scraper:app', workers=2)
