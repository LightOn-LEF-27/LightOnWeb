import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div className="page-msg">
      <h1>페이지를 찾을 수 없습니다</h1>
      <p>
        <Link to="/sessions">세션 목록으로</Link>
      </p>
    </div>
  )
}
