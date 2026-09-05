# 20260903T160103Z-8fec0e75-tap-compare

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
| Q01 | 1 | 17.7 ±0.1 | 0.271 ±0.002 | 163.7 ±0.6 | 0.008 ±0.000 | egernia-local |
| Q01 | 4 | 15.4 ±0.1 | 0.450 ±0.003 | 200.0 ±1.2 | 0.029 ±0.000 | egernia-local |
| Q01 | 8 | 14.7 ±0.0 | 0.680 ±0.005 | 190.6 ±1.2 | 0.060 ±0.000 | egernia-local |
| Q01 | 16 | 13.6 ±0.1 | 1.416 ±0.011 | 184.1 ±1.0 | 0.115 ±0.002 | egernia-local |
| Q01 | 32 | 13.5 ±0.0 | 2.535 ±0.021 | 173.2 ±0.2 | 0.229 ±0.004 | egernia-local |
| Q02 | 1 | 5.2 ±0.0 | 0.407 ±0.002 | 64.4 ±0.2 | 0.023 ±0.001 | egernia-local |
| Q02 | 4 | 10.3 ±0.2 | 0.601 ±0.017 | 137.1 ±1.6 | 0.042 ±0.001 | egernia-local |
| Q02 | 8 | 10.0 ±0.2 | 1.025 ±0.033 | 150.8 ±0.3 | 0.074 ±0.000 | egernia-local |
| Q02 | 16 | 9.6 ±0.0 | 1.988 ±0.077 | 146.9 ±1.1 | 0.143 ±0.001 | egernia-local |
| Q02 | 32 | 9.6 ±0.1 | 3.596 ±0.140 | 138.6 ±0.5 | 0.285 ±0.001 | egernia-local |
| Q03 | 1 | 10.0 ±0.3 | 0.314 ±0.001 | 34.0 ±0.6 | 0.127 ±0.001 | egernia-local |
| Q03 | 4 | 10.8 ±0.7 | 0.585 ±0.018 | 101.8 ±1.8 | 0.158 ±0.001 | egernia-local |
| Q03 | 8 | 10.7 ±0.1 | 0.934 ±0.010 | 137.2 ±0.6 | 0.192 ±0.000 | egernia-local |
| Q03 | 16 | 10.2 ±0.0 | 1.887 ±0.028 | 134.1 ±1.4 | 0.258 ±0.003 | egernia-local |
| Q03 | 32 | 10.2 ±0.1 | 3.368 ±0.075 | 128.3 ±0.5 | 0.391 ±0.005 | egernia-local |
| Q04 | 1 | 10.6 ±0.0 | 0.315 ±0.002 | 46.0 ±0.2 | 0.026 ±0.000 | egernia-local |
| Q04 | 4 | 10.9 ±0.2 | 0.577 ±0.005 | 112.8 ±0.7 | 0.047 ±0.000 | egernia-local |
| Q04 | 8 | 10.5 ±0.2 | 0.943 ±0.033 | 131.6 ±0.8 | 0.081 ±0.001 | egernia-local |
| Q04 | 16 | 10.1 ±0.1 | 1.903 ±0.078 | 133.9 ±0.8 | 0.153 ±0.000 | egernia-local |
| Q04 | 32 | 10.1 ±0.1 | 3.386 ±0.031 | 126.4 ±0.4 | 0.307 ±0.002 | egernia-local |
| Q05 | 1 | 1.8 ±0.1 | 0.786 ±0.017 | 100.5 ±1.5 | 0.013 ±0.001 | egernia-local |
| Q05 | 4 | 6.0 ±0.2 | 0.916 ±0.037 | 175.6 ±2.0 | 0.033 ±0.001 | egernia-local |
| Q05 | 8 | 8.0 ±0.1 | 1.427 ±0.031 | 175.0 ±1.6 | 0.064 ±0.001 | egernia-local |
| Q05 | 16 | 8.9 ±0.2 | 2.340 ±0.169 | 165.5 ±0.5 | 0.127 ±0.001 | egernia-local |
| Q05 | 32 | 8.9 ±0.1 | 4.054 ±0.161 | 156.3 ±0.8 | 0.255 ±0.007 | egernia-local |
| Q06 | 1 | 1.8 ±0.0 | 0.793 ±0.017 | 56.6 ±0.6 | 0.024 ±0.001 | egernia-local |
| Q06 | 4 | 5.9 ±0.1 | 0.925 ±0.027 | 126.9 ±2.5 | 0.045 ±0.000 | egernia-local |
| Q06 | 8 | 7.8 ±0.2 | 1.454 ±0.081 | 139.0 ±1.4 | 0.080 ±0.001 | egernia-local |
| Q06 | 16 | 8.6 ±0.1 | 2.450 ±0.159 | 134.6 ±2.0 | 0.155 ±0.002 | egernia-local |
| Q06 | 32 | 8.5 ±0.8 | 4.238 ±0.474 | 127.8 ±0.9 | 0.306 ±0.005 | egernia-local |
| Q07 | 1 | 1.8 ±0.0 | 0.790 ±0.004 | 77.2 ±0.4 | 0.018 ±0.000 | egernia-local |
| Q07 | 4 | 5.9 ±0.2 | 0.923 ±0.041 | 156.9 ±2.3 | 0.037 ±0.001 | egernia-local |
| Q07 | 8 | 7.7 ±0.1 | 1.478 ±0.029 | 163.9 ±0.5 | 0.069 ±0.001 | egernia-local |
| Q07 | 16 | 8.5 ±0.2 | 2.392 ±0.104 | 156.2 ±0.5 | 0.135 ±0.001 | egernia-local |
| Q07 | 32 | 8.5 ±0.0 | 4.143 ±0.054 | 148.7 ±0.6 | 0.266 ±0.007 | egernia-local |
| Q10 | 1 | 9.2 ±0.1 | 0.330 ±0.001 | 30.5 ±0.2 | 0.039 ±0.001 | egernia-local |
| Q10 | 4 | 9.6 ±0.1 | 0.632 ±0.010 | 75.7 ±0.4 | 0.070 ±0.001 | egernia-local |
| Q10 | 8 | 9.4 ±0.1 | 1.044 ±0.029 | 87.5 ±0.5 | 0.118 ±0.002 | egernia-local |
| Q10 | 16 | 9.0 ±0.6 | 2.173 ±0.092 | 87.0 ±0.8 | 0.236 ±0.002 | egernia-local |
| Q10 | 32 | 9.1 ±0.1 | 3.771 ±0.071 | 83.8 ±0.6 | 0.457 ±0.003 | egernia-local |
| Q11 | 1 | 3.3 ±0.0 | 0.528 ±0.008 | 4.3 ±0.2 | 0.262 ±0.011 | egernia-local |
| Q11 | 4 | 3.7 ±0.0 | 1.336 ±0.072 | 12.1 ±0.2 | 0.408 ±0.006 | egernia-local |
| Q11 | 8 | 3.7 ±0.1 | 2.803 ±0.111 | 15.6 ±0.1 | 0.609 ±0.003 | egernia-local |
| Q11 | 16 | 3.9 ±0.0 | 6.048 ±0.873 | 16.7 ±0.1 | 1.142 ±0.016 | egernia-local |
| Q11 | 32 | 4.0 ±0.1 | 12.667 ±2.067 | 15.6 ±3.6 (4% err) | 3.182 ±3.931 | dachs-local |
| Q12 | 1 | 1.8 ±0.0 | 0.783 ±0.017 | 105.6 ±1.9 | 0.013 ±0.001 | egernia-local |
| Q12 | 4 | 6.0 ±0.2 | 0.900 ±0.048 | 180.1 ±1.9 | 0.032 ±0.000 | egernia-local |
| Q12 | 8 | 7.9 ±0.3 | 1.453 ±0.096 | 178.3 ±0.4 | 0.063 ±0.000 | egernia-local |
| Q12 | 16 | 8.9 ±0.2 | 2.290 ±0.104 | 168.8 ±0.8 | 0.125 ±0.001 | egernia-local |
| Q12 | 32 | 8.9 ±0.2 | 4.050 ±0.133 | 158.9 ±2.1 | 0.250 ±0.004 | egernia-local |
| Q13 | 1 | 3.0 ±0.1 | 0.563 ±0.016 | 4.2 ±0.1 | 0.329 ±0.004 | egernia-local |
| Q13 | 4 | 8.5 ±0.2 | 0.758 ±0.010 | 9.5 ±0.2 | 0.569 ±0.015 | egernia-local |
| Q13 | 8 | 10.0 ±0.2 | 1.188 ±0.026 | 9.7 ±0.2 | 1.106 ±0.029 | tie |
| Q13 | 16 | 10.0 ±0.1 | 2.124 ±0.022 | 9.8 ±0.1 | 1.974 ±0.035 | tie |
| Q13 | 32 | 10.1 ±0.3 | 3.531 ±0.148 | 9.7 ±0.1 | 3.685 ±0.061 | tie |
| mix | 1 | 3.0 ±0.3 | 0.605 ±0.096 | 56.2 ±1.3 | 0.033 ±0.000 | egernia-local |
| mix | 4 | 8.6 ±0.0 | 0.923 ±0.032 | 137.9 ±1.6 | 0.050 ±0.001 | egernia-local |
| mix | 8 | 9.9 ±0.2 | 1.299 ±0.082 | 149.5 ±0.4 | 0.084 ±0.001 | egernia-local |
| mix | 16 | 9.4 ±0.4 | 2.279 ±0.126 | 144.6 ±0.4 | 0.159 ±0.003 | egernia-local |
| mix | 32 | 9.5 ±0.1 | 3.931 ±0.130 | 136.4 ±0.4 | 0.318 ±0.004 | egernia-local |

## votable

| class | c | dachs-local rps | dachs-local p95 (s) | egernia-local rps | egernia-local p95 (s) | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Q01 | 1 | 18.0 ±0.6 | 0.216 ±0.016 | 161.9 ±0.9 | 0.008 ±0.000 | egernia-local |
| Q01 | 4 | 15.6 ±0.4 | 0.423 ±0.012 | 198.4 ±1.2 | 0.029 ±0.000 | egernia-local |
| Q01 | 8 | 15.0 ±0.2 | 0.661 ±0.006 | 190.0 ±0.2 | 0.060 ±0.000 | egernia-local |
| Q01 | 16 | 13.5 ±0.3 | 1.407 ±0.027 | 182.4 ±1.7 | 0.116 ±0.002 | egernia-local |
| Q01 | 32 | 13.1 ±0.6 | 2.703 ±0.468 | 171.4 ±0.7 | 0.233 ±0.002 | egernia-local |
| Q02 | 1 | 5.1 ±0.0 | 0.410 ±0.006 | 62.9 ±0.6 | 0.024 ±0.000 | egernia-local |
| Q02 | 4 | 10.2 ±0.2 | 0.614 ±0.016 | 134.4 ±0.3 | 0.043 ±0.000 | egernia-local |
| Q02 | 8 | 10.0 ±0.0 | 1.029 ±0.020 | 148.8 ±0.4 | 0.075 ±0.000 | egernia-local |
| Q02 | 16 | 9.6 ±0.0 | 1.983 ±0.014 | 144.7 ±0.8 | 0.144 ±0.002 | egernia-local |
| Q02 | 32 | 9.6 ±0.1 | 3.600 ±0.057 | 136.6 ±0.8 | 0.287 ±0.006 | egernia-local |
| Q03 | 1 | 8.6 ±4.6 | 0.363 ±0.195 | 33.8 ±0.3 | 0.126 ±0.001 | egernia-local |
| Q03 | 4 | 10.7 ±0.1 | 0.589 ±0.002 | 101.4 ±0.9 | 0.156 ±0.001 | egernia-local |
| Q03 | 8 | 10.6 ±0.0 | 0.950 ±0.016 | 135.6 ±1.3 | 0.190 ±0.002 | egernia-local |
| Q03 | 16 | 10.0 ±0.1 | 1.911 ±0.080 | 132.9 ±1.4 | 0.256 ±0.001 | egernia-local |
| Q03 | 32 | 10.1 ±0.0 | 3.389 ±0.022 | 126.8 ±2.4 | 0.392 ±0.007 | egernia-local |
| Q04 | 1 | 10.2 ±0.0 | 0.319 ±0.001 | 44.6 ±0.3 | 0.027 ±0.000 | egernia-local |
| Q04 | 4 | 10.6 ±0.1 | 0.580 ±0.002 | 108.1 ±1.0 | 0.049 ±0.001 | egernia-local |
| Q04 | 8 | 10.5 ±0.0 | 0.943 ±0.010 | 126.3 ±0.3 | 0.084 ±0.000 | egernia-local |
| Q04 | 16 | 10.0 ±0.2 | 1.939 ±0.112 | 127.8 ±0.5 | 0.161 ±0.002 | egernia-local |
| Q04 | 32 | 10.0 ±0.1 | 3.429 ±0.035 | 121.3 ±0.7 | 0.319 ±0.004 | egernia-local |
| Q05 | 1 | 1.8 ±0.0 | 0.785 ±0.009 | 99.2 ±0.3 | 0.014 ±0.000 | egernia-local |
| Q05 | 4 | 6.0 ±0.2 | 0.897 ±0.030 | 173.5 ±1.5 | 0.033 ±0.000 | egernia-local |
| Q05 | 8 | 7.7 ±0.1 | 1.478 ±0.062 | 173.0 ±0.2 | 0.065 ±0.001 | egernia-local |
| Q05 | 16 | 8.7 ±0.5 | 2.372 ±0.210 | 163.7 ±1.1 | 0.129 ±0.003 | egernia-local |
| Q05 | 32 | 8.7 ±0.1 | 4.139 ±0.113 | 154.4 ±3.5 | 0.257 ±0.005 | egernia-local |
| Q06 | 1 | 1.7 ±0.0 | 0.800 ±0.009 | 54.0 ±0.9 | 0.025 ±0.000 | egernia-local |
| Q06 | 4 | 5.8 ±0.0 | 0.921 ±0.028 | 123.7 ±5.0 | 0.047 ±0.002 | egernia-local |
| Q06 | 8 | 7.7 ±0.2 | 1.469 ±0.068 | 135.0 ±0.2 | 0.082 ±0.001 | egernia-local |
| Q06 | 16 | 8.6 ±0.1 | 2.411 ±0.075 | 131.5 ±0.7 | 0.161 ±0.001 | egernia-local |
| Q06 | 32 | 8.6 ±0.1 | 4.160 ±0.273 | 124.5 ±0.9 | 0.315 ±0.001 | egernia-local |
| Q07 | 1 | 1.7 ±0.0 | 0.787 ±0.018 | 75.5 ±0.1 | 0.019 ±0.000 | egernia-local |
| Q07 | 4 | 5.9 ±0.1 | 0.925 ±0.005 | 154.8 ±2.7 | 0.038 ±0.000 | egernia-local |
| Q07 | 8 | 7.7 ±0.0 | 1.470 ±0.032 | 161.4 ±1.1 | 0.070 ±0.001 | egernia-local |
| Q07 | 16 | 8.3 ±0.1 | 2.439 ±0.095 | 154.3 ±0.3 | 0.137 ±0.001 | egernia-local |
| Q07 | 32 | 8.3 ±0.2 | 4.257 ±0.090 | 145.8 ±1.4 | 0.271 ±0.002 | egernia-local |
| Q10 | 1 | 9.0 ±0.0 | 0.333 ±0.001 | 28.5 ±0.3 | 0.042 ±0.001 | egernia-local |
| Q10 | 4 | 9.5 ±0.3 | 0.633 ±0.009 | 69.9 ±0.5 | 0.075 ±0.001 | egernia-local |
| Q10 | 8 | 9.6 ±0.1 | 1.038 ±0.019 | 80.9 ±0.8 | 0.127 ±0.002 | egernia-local |
| Q10 | 16 | 9.3 ±0.1 | 2.108 ±0.036 | 81.2 ±0.6 | 0.255 ±0.001 | egernia-local |
| Q10 | 32 | 9.3 ±0.1 | 3.726 ±0.094 | 78.4 ±0.5 | 0.489 ±0.006 | egernia-local |
| Q11 | 1 | 3.5 ±0.0 | 0.505 ±0.003 | 4.0 ±0.0 | 0.275 ±0.002 | egernia-local |
| Q11 | 4 | 4.2 ±0.0 | 1.224 ±0.058 | 11.0 ±0.4 | 0.447 ±0.022 | egernia-local |
| Q11 | 8 | 4.2 ±0.1 | 2.470 ±0.066 | 14.0 ±0.2 | 0.680 ±0.008 | egernia-local |
| Q11 | 16 | 4.3 ±0.0 | 5.334 ±0.664 | 15.0 ±0.0 | 1.246 ±0.044 | egernia-local |
| Q11 | 32 | 4.4 ±0.1 | 11.701 ±1.588 | 14.8 ±0.0 | 2.497 ±0.031 | egernia-local |
| Q12 | 1 | 1.8 ±0.0 | 0.790 ±0.013 | 104.7 ±0.3 | 0.013 ±0.000 | egernia-local |
| Q12 | 4 | 5.9 ±0.1 | 0.910 ±0.041 | 178.3 ±1.4 | 0.032 ±0.001 | egernia-local |
| Q12 | 8 | 7.9 ±0.3 | 1.450 ±0.070 | 176.5 ±0.6 | 0.064 ±0.000 | egernia-local |
| Q12 | 16 | 8.8 ±0.1 | 2.351 ±0.163 | 167.5 ±0.9 | 0.126 ±0.001 | egernia-local |
| Q12 | 32 | 8.8 ±0.2 | 4.070 ±0.171 | 158.0 ±0.6 | 0.250 ±0.002 | egernia-local |
| Q13 | 1 | 3.0 ±0.1 | 0.572 ±0.046 | 4.1 ±0.0 | 0.332 ±0.003 | egernia-local |
| Q13 | 4 | 8.4 ±0.4 | 0.762 ±0.032 | 9.4 ±0.2 | 0.570 ±0.010 | egernia-local |
| Q13 | 8 | 10.0 ±0.2 | 1.184 ±0.013 | 9.7 ±0.2 | 1.118 ±0.010 | tie |
| Q13 | 16 | 9.8 ±0.2 | 2.180 ±0.066 | 9.7 ±0.1 | 1.996 ±0.014 | tie |
| Q13 | 32 | 10.0 ±0.2 | 3.567 ±0.088 | 9.7 ±0.1 | 3.711 ±0.034 | tie |
| mix | 1 | 3.0 ±0.2 | 0.584 ±0.008 | 55.5 ±1.1 | 0.034 ±0.000 | egernia-local |
| mix | 4 | 8.9 ±0.3 | 0.851 ±0.019 | 137.2 ±1.8 | 0.052 ±0.001 | egernia-local |
| mix | 8 | 10.4 ±0.2 | 1.237 ±0.055 | 146.1 ±0.4 | 0.089 ±0.001 | egernia-local |
| mix | 16 | 10.0 ±0.2 | 2.107 ±0.044 | 140.4 ±0.5 | 0.171 ±0.003 | egernia-local |
| mix | 32 | 10.0 ±0.1 | 3.711 ±0.018 | 132.9 ±0.8 | 0.325 ±0.005 | egernia-local |

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

Environment: see `environment.json` (git 8fec0e75, seed 424242, corpus 9f9da00f45f1…).

## Run notes

- Versions: egernia `main` 8fec0e7 = the previous published run (3c8add8)
  plus PR #144 (trigram index on the ObsCore publisher DID, removable
  access join, ancestor foreign keys), PR #145 (VOTable TABLEDATA rendered
  in PostgreSQL) and PR #146 (PostgreSQL sized to the container:
  `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=64MB`,
  `max_parallel_workers=32`, `max_worker_processes=40`; verified with
  `SHOW` before launch). DaCHS image and pins unchanged.
- Corpus: egernia re-seeded from scratch on 2026-09-02 on the #144 image, so
  the API bootstrap created the trigram index and the foreign keys before
  the load: 500,096 products in 25.0 min with the GIN index and keys live
  (the four GiST footprint indexes set aside and rebuilt in 49.7 min;
  seed total 75.7 min). The exported `ivoa.obscore` sha256 (`bc411050…`) is
  byte-identical to the 2026-09-01 corpus. DaCHS volumes were dropped and
  re-ingested from that export (80.5 min). For this run the egernia stack
  was recreated on the same volume with the #145/#146 images.
- Verdicts: egernia 113 cells, DaCHS 1, ties 6. Q02 is egernia's at every
  concurrency (150.8 rps at c=8 against 10.0; 2.3 in the previous run).
  Q13 is egernia's at c=1 and 4 and a tie at c=8–32 (9.7 against 10.0 rps;
  0.7 rps with 58–84% HTTP 503 before). The mixed workload runs at
  149.5 rps at c=8 against 9.9 (12.8 before) with the lower p95 throughout.
  DaCHS's one cell, CSV Q11 at c=32: one of egernia's three repetitions
  shed 71 requests with HTTP 503 (4% of the cell's requests; over 1%
  forfeits the cell by the pre-registered rule). The other two CSV
  repetitions and all three VOTable repetitions had zero errors.
- Box: 30-CPU host. A second, CPU-isolated egernia stack (a tuning
  experiment on CPUs 16–23) was up but idle when this run launched and had
  been taken down by its end; it shared the disk and memory bandwidth, not
  the pinned cores. An earlier run of this protocol on #144-only `main`
  (`20260902T223530Z-32112952-tap-compare`) was stopped at rung 408/720
  when #145/#146 merged; its results directory is kept, unpublished.
