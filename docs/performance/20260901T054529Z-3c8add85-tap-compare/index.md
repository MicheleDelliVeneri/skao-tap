# 20260901T054529Z-3c8add85-tap-compare

Same-hardware TAP-server comparison: identical logical corpus, each
server deployed per its own documentation, one target under load at
a time (all stacks stay up so repetitions interleave), the identical
seeded query stream, MAXREC pinned on every request. Every target
stack is pinned to the same 8 CPU / 8 GiB
budget: DaCHS in `benchmarks/tap-compare/docker-compose.dachs.yml`
(`cpus: 8`, `mem_limit: 8g`), egernia in
`benchmarks/tap-compare/docker-compose.egernia-pins.yml` (shared
`cpuset` of 8 cores; 8 GiB split 4 db / 2 api / 2 executor).
See `benchmarks/tap-compare/README.md` for the protocol.

## Gates

| target | TAP | taplint | maxrec default |
| --- | --- | --- | --- |
| dachs-local | 1.1 | PASS (0 errors) | 20000 |
| egernia-local | 1.1 | PASS (0 errors) | 10000 |

Agreement gate: **11 classes agree**, none disagree.

## csv

| class | c | dachs-local rps | dachs-local p95 (s) | egernia-local rps | egernia-local p95 (s) | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Q01 | 1 | 19.9 ±0.1 | 0.203 ±0.001 | 165.1 ±1.4 | 0.008 ±0.000 | egernia-local |
| Q01 | 4 | 17.0 ±0.3 | 0.381 ±0.008 | 202.6 ±0.3 | 0.028 ±0.000 | egernia-local |
| Q01 | 8 | 16.1 ±0.9 | 0.618 ±0.047 | 193.1 ±0.3 | 0.059 ±0.000 | egernia-local |
| Q01 | 16 | 14.9 ±0.1 | 1.280 ±0.030 | 185.8 ±1.1 | 0.114 ±0.001 | egernia-local |
| Q01 | 32 | 14.9 ±0.3 | 2.297 ±0.049 | 175.2 ±0.5 | 0.226 ±0.004 | egernia-local |
| Q02 | 1 | 5.4 ±0.0 | 0.340 ±0.003 | 0.5 ±0.0 | 2.118 ±0.029 | dachs-local |
| Q02 | 4 | 11.0 ±0.2 | 0.536 ±0.016 | 1.5 ±0.1 | 3.362 ±0.224 | dachs-local |
| Q02 | 8 | 10.7 ±0.1 | 0.956 ±0.031 | 2.3 ±0.0 | 3.588 ±0.015 | dachs-local |
| Q02 | 16 | 10.3 ±0.1 | 1.854 ±0.031 | 2.3 ±0.0 | 7.134 ±0.013 | dachs-local |
| Q02 | 32 | 10.3 ±0.1 | 3.378 ±0.058 | 4.9 ±0.1 (54% err) | 8.923 ±0.452 | dachs-local |
| Q03 | 1 | 10.4 ±0.5 | 0.245 ±0.002 | 21.4 ±1.3 | 0.244 ±0.001 | egernia-local |
| Q03 | 4 | 11.7 ±0.1 | 0.517 ±0.011 | 64.4 ±2.3 | 0.320 ±0.003 | egernia-local |
| Q03 | 8 | 11.4 ±0.1 | 0.876 ±0.024 | 94.8 ±0.6 | 0.399 ±0.001 | egernia-local |
| Q03 | 16 | 10.9 ±0.1 | 1.755 ±0.057 | 94.1 ±0.8 | 0.499 ±0.005 | egernia-local |
| Q03 | 32 | 10.9 ±0.1 | 3.165 ±0.042 | 91.8 ±0.9 | 0.688 ±0.004 | egernia-local |
| Q04 | 1 | 11.3 ±0.0 | 0.246 ±0.002 | 31.3 ±0.2 | 0.039 ±0.000 | egernia-local |
| Q04 | 4 | 11.8 ±0.2 | 0.505 ±0.010 | 70.5 ±0.4 | 0.073 ±0.001 | egernia-local |
| Q04 | 8 | 11.4 ±0.1 | 0.865 ±0.015 | 92.7 ±0.2 | 0.113 ±0.000 | egernia-local |
| Q04 | 16 | 10.8 ±0.0 | 1.750 ±0.040 | 94.2 ±0.7 | 0.216 ±0.003 | egernia-local |
| Q04 | 32 | 10.8 ±0.1 | 3.163 ±0.104 | 91.4 ±0.2 | 0.416 ±0.006 | egernia-local |
| Q05 | 1 | 1.8 ±0.0 | 0.710 ±0.014 | 82.5 ±0.4 | 0.016 ±0.000 | egernia-local |
| Q05 | 4 | 6.2 ±0.3 | 0.830 ±0.088 | 164.2 ±1.6 | 0.035 ±0.000 | egernia-local |
| Q05 | 8 | 8.7 ±0.2 | 1.270 ±0.043 | 173.0 ±0.2 | 0.065 ±0.000 | egernia-local |
| Q05 | 16 | 9.7 ±0.3 | 2.013 ±0.128 | 165.2 ±0.9 | 0.127 ±0.003 | egernia-local |
| Q05 | 32 | 9.8 ±0.3 | 3.621 ±0.287 | 156.5 ±0.6 | 0.250 ±0.004 | egernia-local |
| Q06 | 1 | 1.8 ±0.0 | 0.724 ±0.007 | 57.2 ±0.7 | 0.024 ±0.000 | egernia-local |
| Q06 | 4 | 6.2 ±0.2 | 0.828 ±0.057 | 127.8 ±1.7 | 0.044 ±0.001 | egernia-local |
| Q06 | 8 | 8.5 ±0.2 | 1.276 ±0.028 | 140.4 ±0.4 | 0.079 ±0.001 | egernia-local |
| Q06 | 16 | 9.3 ±0.3 | 2.135 ±0.088 | 136.4 ±0.2 | 0.154 ±0.001 | egernia-local |
| Q06 | 32 | 9.4 ±0.1 | 3.726 ±0.092 | 129.5 ±0.8 | 0.304 ±0.005 | egernia-local |
| Q07 | 1 | 1.8 ±0.0 | 0.711 ±0.022 | 77.3 ±0.4 | 0.018 ±0.000 | egernia-local |
| Q07 | 4 | 6.2 ±0.3 | 0.838 ±0.057 | 158.6 ±1.8 | 0.037 ±0.001 | egernia-local |
| Q07 | 8 | 8.4 ±0.3 | 1.310 ±0.075 | 165.2 ±1.0 | 0.068 ±0.001 | egernia-local |
| Q07 | 16 | 9.0 ±0.1 | 2.147 ±0.039 | 159.6 ±2.6 | 0.132 ±0.002 | egernia-local |
| Q07 | 32 | 9.2 ±0.2 | 3.762 ±0.086 | 151.7 ±1.4 | 0.262 ±0.003 | egernia-local |
| Q10 | 1 | 9.5 ±0.1 | 0.263 ±0.003 | 29.6 ±1.0 | 0.043 ±0.001 | egernia-local |
| Q10 | 4 | 10.2 ±0.4 | 0.567 ±0.023 | 75.6 ±5.9 | 0.070 ±0.007 | egernia-local |
| Q10 | 8 | 9.9 ±0.4 | 0.995 ±0.059 | 89.0 ±1.9 | 0.116 ±0.006 | egernia-local |
| Q10 | 16 | 9.7 ±0.1 | 1.960 ±0.055 | 88.9 ±0.2 | 0.231 ±0.001 | egernia-local |
| Q10 | 32 | 9.7 ±0.5 | 3.605 ±0.420 | 85.4 ±1.3 | 0.450 ±0.012 | egernia-local |
| Q11 | 1 | 3.3 ±0.1 | 0.461 ±0.008 | 4.6 ±0.0 | 0.244 ±0.010 | egernia-local |
| Q11 | 4 | 3.7 ±0.3 | 1.305 ±0.115 | 12.3 ±0.1 | 0.399 ±0.010 | egernia-local |
| Q11 | 8 | 3.8 ±0.1 | 2.749 ±0.073 | 15.4 ±0.2 | 0.610 ±0.022 | egernia-local |
| Q11 | 16 | 4.0 ±0.1 | 6.026 ±0.760 | 16.8 ±0.0 | 1.123 ±0.011 | egernia-local |
| Q11 | 32 | 4.0 ±0.1 | 12.050 ±1.296 | 16.6 ±0.1 | 2.216 ±0.015 | egernia-local |
| Q12 | 1 | 1.8 ±0.0 | 0.722 ±0.002 | 104.1 ±0.2 | 0.013 ±0.000 | egernia-local |
| Q12 | 4 | 6.2 ±0.2 | 0.818 ±0.030 | 179.5 ±6.9 | 0.032 ±0.001 | egernia-local |
| Q12 | 8 | 8.6 ±0.2 | 1.259 ±0.061 | 176.8 ±1.5 | 0.064 ±0.001 | egernia-local |
| Q12 | 16 | 9.7 ±0.2 | 2.032 ±0.042 | 167.8 ±1.6 | 0.126 ±0.002 | egernia-local |
| Q12 | 32 | 9.7 ±0.4 | 3.742 ±0.518 | 159.6 ±2.1 | 0.248 ±0.005 | egernia-local |
| Q13 | 1 | 3.1 ±0.1 | 0.487 ±0.013 | 0.1 ±0.0 | 18.873 ±3.631 | dachs-local |
| Q13 | 4 | 9.2 ±0.2 | 0.684 ±0.028 | 0.5 ±0.0 | 14.105 ±2.627 | dachs-local |
| Q13 | 8 | 11.0 ±0.2 | 1.065 ±0.027 | 0.7 ±0.1 | 20.674 ±3.832 | dachs-local |
| Q13 | 16 | 10.8 ±0.1 | 1.909 ±0.045 | 1.5 ±0.2 (58% err) | 23.488 ±5.752 | dachs-local |
| Q13 | 32 | 10.8 ±0.3 | 3.280 ±0.079 | 4.1 ±0.4 (84% err) | 18.111 ±2.253 | dachs-local |
| mix | 1 | 3.1 ±0.2 | 0.586 ±0.016 | 2.9 ±0.7 | 2.059 ±0.023 | tie |
| mix | 4 | 9.0 ±0.1 | 0.852 ±0.024 | 8.9 ±1.0 | 2.962 ±0.138 | tie |
| mix | 8 | 10.2 ±0.1 | 1.243 ±0.075 | 12.8 ±1.3 | 3.854 ±0.207 | egernia-local |
| mix | 16 | 9.9 ±0.5 | 2.137 ±0.178 | 12.5 ±0.1 | 4.491 ±1.087 | egernia-local |
| mix | 32 | 10.1 ±0.2 | 3.700 ±0.047 | 12.5 ±1.6 | 6.402 ±0.587 | egernia-local |

## votable

| class | c | dachs-local rps | dachs-local p95 (s) | egernia-local rps | egernia-local p95 (s) | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Q01 | 1 | 20.9 ±0.7 | 0.139 ±0.018 | 175.7 ±0.3 | 0.008 ±0.000 | egernia-local |
| Q01 | 4 | 17.7 ±0.3 | 0.342 ±0.009 | 213.0 ±1.6 | 0.027 ±0.000 | egernia-local |
| Q01 | 8 | 16.9 ±0.5 | 0.587 ±0.031 | 204.1 ±1.1 | 0.056 ±0.001 | egernia-local |
| Q01 | 16 | 15.0 ±0.3 | 1.257 ±0.042 | 197.3 ±0.6 | 0.108 ±0.001 | egernia-local |
| Q01 | 32 | 14.8 ±0.3 | 2.321 ±0.044 | 185.9 ±0.3 | 0.215 ±0.003 | egernia-local |
| Q02 | 1 | 5.3 ±0.0 | 0.341 ±0.002 | 0.5 ±0.0 | 2.093 ±0.091 | dachs-local |
| Q02 | 4 | 11.1 ±0.1 | 0.533 ±0.002 | 1.4 ±0.1 | 3.398 ±0.274 | dachs-local |
| Q02 | 8 | 10.7 ±0.2 | 0.948 ±0.017 | 2.3 ±0.0 | 3.590 ±0.014 | dachs-local |
| Q02 | 16 | 10.3 ±0.0 | 1.838 ±0.057 | 2.3 ±0.0 | 7.137 ±0.036 | dachs-local |
| Q02 | 32 | 10.3 ±0.1 | 3.343 ±0.036 | 4.9 ±0.1 (54% err) | 8.952 ±0.849 | dachs-local |
| Q03 | 1 | 10.3 ±0.3 | 0.246 ±0.001 | 21.7 ±1.7 | 0.241 ±0.003 | egernia-local |
| Q03 | 4 | 11.5 ±0.1 | 0.513 ±0.009 | 64.5 ±1.1 | 0.320 ±0.004 | egernia-local |
| Q03 | 8 | 11.3 ±0.0 | 0.874 ±0.010 | 93.3 ±0.8 | 0.400 ±0.003 | egernia-local |
| Q03 | 16 | 10.8 ±0.0 | 1.753 ±0.029 | 92.7 ±1.0 | 0.498 ±0.009 | egernia-local |
| Q03 | 32 | 10.9 ±0.2 | 3.154 ±0.035 | 89.7 ±1.6 | 0.690 ±0.009 | egernia-local |
| Q04 | 1 | 11.0 ±0.1 | 0.248 ±0.002 | 30.8 ±0.1 | 0.040 ±0.000 | egernia-local |
| Q04 | 4 | 11.4 ±0.1 | 0.505 ±0.007 | 61.2 ±0.2 | 0.082 ±0.001 | egernia-local |
| Q04 | 8 | 11.3 ±0.2 | 0.869 ±0.003 | 78.5 ±0.3 | 0.129 ±0.001 | egernia-local |
| Q04 | 16 | 10.8 ±0.1 | 1.758 ±0.060 | 79.9 ±0.8 | 0.252 ±0.002 | egernia-local |
| Q04 | 32 | 10.8 ±0.1 | 3.182 ±0.057 | 78.0 ±0.5 | 0.486 ±0.003 | egernia-local |
| Q05 | 1 | 1.8 ±0.0 | 0.713 ±0.015 | 95.0 ±0.3 | 0.014 ±0.000 | egernia-local |
| Q05 | 4 | 6.3 ±0.1 | 0.796 ±0.009 | 171.7 ±2.9 | 0.034 ±0.001 | egernia-local |
| Q05 | 8 | 8.6 ±0.3 | 1.266 ±0.056 | 178.5 ±0.7 | 0.063 ±0.000 | egernia-local |
| Q05 | 16 | 9.7 ±0.1 | 2.018 ±0.077 | 170.3 ±0.9 | 0.123 ±0.000 | egernia-local |
| Q05 | 32 | 9.7 ±0.3 | 3.602 ±0.130 | 160.8 ±0.3 | 0.246 ±0.001 | egernia-local |
| Q06 | 1 | 1.8 ±0.0 | 0.716 ±0.017 | 55.5 ±0.5 | 0.026 ±0.000 | egernia-local |
| Q06 | 4 | 6.2 ±0.2 | 0.826 ±0.036 | 102.6 ±0.2 | 0.057 ±0.000 | egernia-local |
| Q06 | 8 | 8.5 ±0.2 | 1.274 ±0.059 | 109.9 ±0.7 | 0.104 ±0.001 | egernia-local |
| Q06 | 16 | 9.4 ±0.2 | 2.093 ±0.082 | 106.1 ±0.7 | 0.203 ±0.003 | egernia-local |
| Q06 | 32 | 9.4 ±0.1 | 3.769 ±0.038 | 102.0 ±1.6 | 0.386 ±0.005 | egernia-local |
| Q07 | 1 | 1.8 ±0.0 | 0.720 ±0.020 | 85.3 ±0.7 | 0.017 ±0.000 | egernia-local |
| Q07 | 4 | 6.2 ±0.2 | 0.825 ±0.044 | 153.4 ±1.4 | 0.039 ±0.000 | egernia-local |
| Q07 | 8 | 8.4 ±0.2 | 1.307 ±0.052 | 156.8 ±0.5 | 0.072 ±0.000 | egernia-local |
| Q07 | 16 | 9.2 ±0.1 | 2.099 ±0.041 | 150.1 ±0.1 | 0.141 ±0.001 | egernia-local |
| Q07 | 32 | 9.2 ±0.1 | 3.781 ±0.086 | 143.0 ±0.2 | 0.276 ±0.002 | egernia-local |
| Q10 | 1 | 9.7 ±0.1 | 0.262 ±0.002 | 25.2 ±0.2 | 0.047 ±0.000 | egernia-local |
| Q10 | 4 | 10.4 ±0.2 | 0.547 ±0.007 | 42.9 ±0.1 | 0.117 ±0.001 | egernia-local |
| Q10 | 8 | 10.3 ±0.1 | 0.959 ±0.020 | 47.7 ±0.1 | 0.217 ±0.002 | egernia-local |
| Q10 | 16 | 9.9 ±0.1 | 1.957 ±0.073 | 47.3 ±0.1 | 0.425 ±0.003 | egernia-local |
| Q10 | 32 | 10.0 ±0.1 | 3.461 ±0.083 | 46.4 ±0.2 | 0.804 ±0.008 | egernia-local |
| Q11 | 1 | 3.7 ±0.0 | 0.434 ±0.006 | 4.3 ±0.0 | 0.274 ±0.006 | egernia-local |
| Q11 | 4 | 4.3 ±0.0 | 1.168 ±0.066 | 6.0 ±0.2 | 0.830 ±0.043 | egernia-local |
| Q11 | 8 | 4.2 ±0.1 | 2.368 ±0.131 | 6.3 ±0.0 | 1.475 ±0.029 | egernia-local |
| Q11 | 16 | 4.5 ±0.1 | 5.170 ±0.172 | 6.2 ±0.0 | 2.920 ±0.050 | egernia-local |
| Q11 | 32 | 4.5 ±0.1 | 11.053 ±1.292 | 6.2 ±0.0 | 5.639 ±0.039 | egernia-local |
| Q12 | 1 | 1.8 ±0.0 | 0.712 ±0.013 | 112.9 ±0.9 | 0.012 ±0.000 | egernia-local |
| Q12 | 4 | 6.3 ±0.1 | 0.814 ±0.028 | 185.3 ±0.7 | 0.031 ±0.000 | egernia-local |
| Q12 | 8 | 8.6 ±0.1 | 1.277 ±0.024 | 182.7 ±3.6 | 0.062 ±0.002 | egernia-local |
| Q12 | 16 | 9.6 ±0.2 | 2.044 ±0.062 | 173.5 ±1.1 | 0.122 ±0.001 | egernia-local |
| Q12 | 32 | 9.6 ±0.2 | 3.636 ±0.092 | 163.2 ±1.1 | 0.243 ±0.005 | egernia-local |
| Q13 | 1 | 2.9 ±0.9 | 0.568 ±0.313 | 0.1 ±0.0 | 18.319 ±1.913 | dachs-local |
| Q13 | 4 | 9.0 ±0.4 | 0.686 ±0.032 | 0.5 ±0.0 | 13.741 ±0.196 | dachs-local |
| Q13 | 8 | 10.8 ±0.1 | 1.086 ±0.040 | 0.7 ±0.1 | 20.397 ±2.628 | dachs-local |
| Q13 | 16 | 10.7 ±0.2 | 1.923 ±0.013 | 1.5 ±0.2 (58% err) | 24.254 ±6.519 | dachs-local |
| Q13 | 32 | 10.7 ±0.1 | 3.321 ±0.053 | 3.8 ±0.3 (84% err) | 18.897 ±1.669 | dachs-local |
| mix | 1 | 3.2 ±0.2 | 0.604 ±0.073 | 2.9 ±0.7 | 2.048 ±0.019 | tie |
| mix | 4 | 9.7 ±0.1 | 0.766 ±0.022 | 8.8 ±1.0 | 2.972 ±0.449 | tie |
| mix | 8 | 11.3 ±0.7 | 1.131 ±0.032 | 12.3 ±1.4 | 3.916 ±0.070 | tie |
| mix | 16 | 11.1 ±0.2 | 1.914 ±0.025 | 12.5 ±1.4 | 4.487 ±0.789 | tie |
| mix | 32 | 10.9 ±0.1 | 3.423 ±0.016 | 12.5 ±1.5 | 6.338 ±0.635 | tie |

## Claims

This run may claim the relative behaviour *of these versions, on this
hardware, on this corpus, as deployed by their own documentation,
under the recorded resource pins* — nothing else. A `tie` verdict is
pre-registered: overlapping 95% intervals, or under 10% apart
in throughput. Classes a gate excluded are absent, not hidden.

## Threats to validity

- The corpus is generated by egernia's own seeder; its distributions
  may flatter egernia's index choices.
- The query classes descend from egernia's own performance history.
- The team operates egernia expertly and DaCHS from its documentation.
- Single hardware, single run window; versions frozen at the recorded
  digests.

## Reproduce with

```bash
scripts/export_obscore_snapshot.sh benchmarks/tap-compare/corpus
docker compose -f docker-compose.yml \
    -f benchmarks/tap-compare/docker-compose.egernia-pins.yml up -d
docker compose -f benchmarks/tap-compare/docker-compose.dachs.yml up -d
uv run --group tap-compare python benchmarks/tap-compare compare \
    --targets dachs-local egernia-local --scenario <scenario>
```

Environment: see `environment.json` (git 3c8add85, seed 424242, corpus 9f9da00f45f1…).
