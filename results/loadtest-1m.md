### mixed: 100 clients, 30s, 22,279 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| GET /balance | 267,009 | 3.2 | 8.3 | 12.7 | 38.3 | 200: 241016, 404: 25993 |
| POST /credit | 233,483 | 4.4 | 9.6 | 14.4 | 39.3 | 200: 233483 |
| POST /debit | 167,889 | 4.3 | 9.6 | 14.2 | 43.8 | 200: 151247, 404: 16514, 409: 128 |
unexpected responses (not 200/404/409): 0
### hot: 100 clients, 30s, 6,855 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| GET /balance | 82,434 | 0.6 | 0.8 | 1.0 | 177.9 | 200: 82434 |
| POST /credit | 71,847 | 22.6 | 31.9 | 39.4 | 375.7 | 200: 71847 |
| POST /debit | 51,378 | 22.7 | 31.9 | 38.6 | 374.6 | 200: 51378 |
unexpected responses (not 200/404/409): 0
### isolation: 100 clients, 30s, 14,724 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| hot: GET /balance | 26,904 | 1.1 | 1.9 | 2.7 | 36.1 | 200: 26904 |
| hot: POST /credit | 23,476 | 64.0 | 100.7 | 127.0 | 258.8 | 200: 23476 |
| hot: POST /debit | 16,905 | 63.4 | 100.5 | 125.0 | 256.7 | 200: 16905 |
| others: GET /balance | 149,966 | 1.1 | 1.9 | 2.6 | 36.1 | 200: 149966 |
| others: POST /credit | 131,125 | 1.8 | 2.8 | 3.9 | 39.3 | 200: 131125 |
| others: POST /debit | 93,353 | 1.8 | 2.9 | 4.1 | 38.9 | 200: 93353 |
unexpected responses (not 200/404/409): 0
