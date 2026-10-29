// 내려받기 — 버튼은 모두에게 보이고, 받을 수 있는지는 서버가 정한다 (HEAD 로 먼저 묻는다. 사이트_설계.md §7)

import { useCallback, useEffect, useState } from 'react'

import { fileUrl } from './model'

export interface Notice {
  title: string
  body: string
  kind?: 'error' | 'info' // 기본은 error (빨간 테두리)
}

async function askServer(fileId: string): Promise<Notice | null> {
  let r: Response
  try {
    r = await fetch(fileUrl(fileId), { method: 'HEAD', credentials: 'same-origin' })
  } catch {
    return { title: '내려받기 실패', body: '서버에 연결할 수 없습니다.' }
  }
  if (r.ok) return null
  if (r.status === 403) return { title: '내려받기 권한 없음', body: '내려받기는 팀장만 할 수 있습니다.' }
  if (r.status === 401) return { title: '로그인이 필요합니다', body: '다시 로그인해 주세요.' }
  if (r.status === 404) return { title: '파일을 찾을 수 없습니다', body: '데이터 폴더에 파일이 없을 수 있습니다. DAQ 파트에 알려 주세요.' }
  return { title: '내려받기 실패', body: `서버 응답 ${r.status}` }
}

// 받을 수 있으면 같은 주소로 내려받기 시작 (서버가 첨부 파일로 보낸다 — 화면은 그대로)
function start(fileId: string): void {
  const a = document.createElement('a')
  a.href = fileUrl(fileId)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export function useDownload(): { download: (fileId: string) => void; notice: Notice | null; dismiss: () => void } {
  const [notice, setNotice] = useState<Notice | null>(null)
  const download = useCallback((fileId: string) => {
    askServer(fileId).then((n) => (n ? setNotice(n) : start(fileId)))
  }, [])
  const dismiss = useCallback(() => setNotice(null), []) // 같은 함수를 넘겨야 알림 타이머가 다시 시작되지 않는다
  return { download, notice, dismiss }
}

export function DownloadButton({ fileId, onClick, label = '내려받기' }: { fileId: string; onClick: (id: string) => void; label?: string }) {
  return (
    <button type="button" className="btn btn-sm" onClick={() => onClick(fileId)}>
      {label}
    </button>
  )
}

export function Toast({ notice, onClose }: { notice: Notice | null; onClose: () => void }) {
  useEffect(() => {
    if (!notice) return
    const t = setTimeout(onClose, 6000)
    return () => clearTimeout(t)
  }, [notice, onClose])
  if (!notice) return null
  return (
    <div className={`toast${notice.kind === 'info' ? ' info' : ''}`} role={notice.kind === 'info' ? 'status' : 'alert'}>
      <span className="toast-mark" aria-hidden="true">
        {notice.kind === 'info' ? '✓' : '✕'}
      </span>
      <div className="toast-text">
        <strong>{notice.title}</strong>
        <span>{notice.body}</span>
      </div>
      <button type="button" className="banner-close" aria-label="알림 닫기" onClick={onClose}>
        ×
      </button>
    </div>
  )
}
