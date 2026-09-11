export default function App() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
      <div className="max-w-lg space-y-3 text-center">
        <h1 className="text-3xl font-semibold text-slate-900">{{app_name}}</h1>
        <p className="text-slate-600">{{app_description}}</p>
        <p className="text-sm text-slate-400">
          Edit <code className="rounded bg-slate-200 px-1 py-0.5">src/App.tsx</code> to begin.
        </p>
      </div>
    </main>
  )
}
