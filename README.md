# ETH 언스테이킹 큐 분석기

Ethereum 언스테이킹 큐와 ETH 가격의 상관관계를 분석하여 변곡점을 찾는 대시보드입니다.

![Dashboard Preview](https://img.shields.io/badge/ETH-Unstaking%20Analyzer-black?style=for-the-badge&logo=ethereum)

## 📊 기능

- **Exit Queue vs ETH Price** 차트 오버레이
- **자동 변곡점 감지** (피크/트로프 포인트)
- **상관관계 분석** (Queue-Price 상관계수)
- **매일 자동 업데이트** (GitHub Actions - 매일 오전 7시 KST)
- **한글/영문 UI**

## 🚀 설치 및 배포

### 1. Repository Fork

이 repository를 Fork 하세요.

### 2. GitHub Pages 활성화

1. Settings → Pages로 이동
2. Source: "GitHub Actions" 선택
3. 저장

### 3. 수동 실행 (선택사항)

Actions 탭에서 "Update ETH Unstaking Data" workflow를 수동 실행할 수 있습니다.

## 📁 프로젝트 구조

```
eth-unstaking-dashboard/
├── index.html              # 메인 대시보드
├── data/
│   └── eth_unstaking_data.json  # 데이터 파일 (자동 업데이트)
├── scripts/
│   └── fetch_data.py       # 데이터 수집 스크립트
└── .github/
    └── workflows/
        └── update-data.yml # GitHub Actions 워크플로우
```

## 🔄 데이터 소스

| 소스 | 데이터 | API |
|------|--------|-----|
| ValidatorQueue.com | 언스테이킹 큐 (historical_data.json) | GitHub Raw |
| CoinGecko | ETH 가격 히스토리 | Public API |

## 📈 변곡점 해석

| 시그널 | 의미 | 트레이딩 힌트 |
|--------|------|---------------|
| 🔺 Queue Peak + Price High | 이익실현 매도 압력 | 주의 필요 |
| 🔻 Queue Trough + Price Low | 매도 소진, 바닥 형성 | 매수 기회? |
| Entry > Exit | 강세 전환 신호 | 긍정적 |

## ⚙️ 로컬 실행

```bash
# 데이터 수집 스크립트 실행
cd scripts
pip install requests
python fetch_data.py

# 로컬 서버 실행
cd ..
python -m http.server 8000
# http://localhost:8000 접속
```

## 📝 업데이트 일정

- **자동 업데이트**: 매일 오전 7:00 KST (22:00 UTC)
- **수동 업데이트**: Actions 탭에서 workflow_dispatch 실행

## ⚠️ 주의사항

- 이 도구는 **투자 조언이 아닙니다**
- 참고용으로만 사용하세요
- Exit 대기 시간이 길면 (40일+) 즉각적인 가격 영향이 제한됩니다

## 📜 라이선스

MIT License

---

**Data Sources**: [ValidatorQueue.com](https://validatorqueue.com) | [CoinGecko](https://coingecko.com)
