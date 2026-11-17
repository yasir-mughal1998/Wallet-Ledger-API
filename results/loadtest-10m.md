### mixed: 100 clients, 30s, 21,459 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| GET /balance | 257,178 | 3.3 | 8.6 | 13.1 | 36.2 | 200: 231946, 404: 25232 |
| POST /credit | 225,246 | 4.5 | 10.1 | 14.6 | 40.6 | 200: 225246 |
| POST /debit | 161,332 | 4.4 | 9.9 | 14.4 | 36.5 | 200: 145421, 404: 15790, 409: 121 |
unexpected responses (not 200/404/409): 0
### hot: 100 clients, 30s, 6,896 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| GET /balance | 82,997 | 0.6 | 0.8 | 1.0 | 23.3 | 200: 82997 |
| POST /credit | 72,311 | 25.6 | 34.5 | 42.2 | 111.8 | 200: 72311 |
| POST /debit | 51,565 | 26.0 | 34.3 | 42.0 | 110.7 | 200: 51565 |
unexpected responses (not 200/404/409): 0
### isolation: 100 clients, 30s, 13,846 req/s
| endpoint | requests | p50 ms | p95 ms | p99 ms | max ms | statuses |
|---|---:|---:|---:|---:|---:|---|
| hot: GET /balance | 24,827 | 1.1 | 2.0 | 2.7 | 66.4 | 200: 24827 |
| hot: POST /credit | 21,705 | 60.1 | 99.0 | 151.2 | 291.9 | 200: 21705 |
| hot: POST /debit | 15,710 | 60.0 | 100.8 | 152.5 | 276.0 | 200: 15710 |
| others: GET /balance | 141,362 | 1.1 | 2.0 | 2.8 | 72.2 | 200: 141362 |
| others: POST /credit | 123,554 | 1.9 | 3.1 | 4.7 | 69.6 | 200: 123554 |
| others: POST /debit | 88,212 | 1.9 | 3.1 | 4.8 | 67.9 | 200: 88212 |
unexpected responses (not 200/404/409): 0
