import type { Metadata } from 'next';
import Link from 'next/link';

export const metadata: Metadata = {
  title: 'Claude Code UI | William老师的AI梦工厂',
  description:
    'Claude Code UI turns Claude Code execution traces into a visual, auditable workflow.',
};

const DOWNLOAD_PATH = '/downloads/cc-flow-src.zip';
const HERO_GIF_PATH = '/claude-code-ui/claude-code-ai.gif';
const RUN_SAMPLE_PATHS = [
  '/claude-code-ui/run_sample_1.png',
  '/claude-code-ui/run_sample_2.png',
  '/claude-code-ui/run_sample_3.png',
];

const featureCards = [
  {
    title: 'See the thought process',
    description: '把终端里的线性输出展开为时间线、工具调用、权限确认与上下文流。',
  },
  {
    title: 'Audit the logic',
    description: '用可回放的前端视图检查每一步推理、文件修改和系统边界。',
  },
  {
    title: 'Build faster',
    description: '第一次拿到源码即可本地运行，不需要登录William老师的AI梦工厂。',
  },
];

export default function ClaudeCodeUiPage() {
  return (
    <main className="min-h-screen overflow-hidden bg-[#fbf9f6] text-[#1d1c16]">
      <section className="relative isolate px-6 py-10 sm:px-10 lg:px-16">
        <div className="absolute inset-0 -z-20 bg-[radial-gradient(circle_at_16%_12%,rgba(217,119,87,0.18),transparent_34%),radial-gradient(circle_at_82%_18%,rgba(29,28,22,0.08),transparent_30%),linear-gradient(180deg,#fbf9f6_0%,#f5efe8_100%)]" />
        <div className="absolute left-1/2 top-20 -z-10 h-[32rem] w-[32rem] -translate-x-1/2 rounded-full border border-[#1d1c16]/10 bg-white/35 blur-3xl" />

        <nav className="mx-auto flex max-w-6xl items-center justify-between text-sm">
          <Link href="/" className="font-medium tracking-tight text-[#1d1c16]/70 hover:text-[#1d1c16]">
            ← 返回 AI梦工厂
          </Link>
          <a
            href={DOWNLOAD_PATH}
            download
            className="rounded-full border border-[#1d1c16]/15 bg-white/55 px-4 py-2 text-[#1d1c16]/75 shadow-sm backdrop-blur hover:bg-white"
          >
            下载源码包
          </a>
        </nav>

        <div className="mx-auto grid max-w-6xl gap-12 py-16 lg:grid-cols-[1.05fr_0.95fr] lg:items-center lg:py-24">
          <div>
            <p className="mb-5 inline-flex rounded-full border border-[#d97757]/25 bg-[#d97757]/10 px-4 py-2 text-sm font-medium text-[#a44b2d]">
              Claude Code UI · Source Package
            </p>
            <h1 className="max-w-4xl text-5xl font-semibold tracking-[-0.045em] text-[#1d1c16] sm:text-6xl lg:text-7xl">
              Claude Code UI
            </h1>
            <p className="mt-6 max-w-2xl text-xl leading-8 text-[#4a4740]">
              See the thought process. Audit the logic. Build faster. 这个项目把
              Claude Code 的运行记录从黑盒终端折射成可视化的玻璃匣子。
            </p>
            <div className="mt-10 flex flex-col gap-4 sm:flex-row">
              <a
                href={DOWNLOAD_PATH}
                download
                className="group inline-flex items-center justify-center rounded-full bg-[#1d1c16] px-7 py-4 text-base font-medium text-white shadow-[0_18px_60px_rgba(29,28,22,0.24)] transition hover:-translate-y-0.5 hover:bg-[#2b2922]"
              >
                Download Source Package
                <span className="ml-2 transition group-hover:translate-x-1">→</span>
              </a>
              <a
                href="#setup"
                className="inline-flex items-center justify-center rounded-full border border-[#1d1c16]/15 bg-white/65 px-7 py-4 text-base font-medium text-[#1d1c16] shadow-sm backdrop-blur transition hover:bg-white"
              >
                查看本地运行步骤
              </a>
            </div>
            <div className="mt-7 rounded-2xl border border-[#1d1c16]/10 bg-white/60 p-4 font-mono text-sm text-[#4a4740] shadow-sm backdrop-blur">
              <span className="text-[#d97757]">$</span> unzip cc-flow-src.zip && cd cc-flow-src && pnpm install && pnpm dev
            </div>
          </div>

          <div className="rounded-[2rem] border border-[#1d1c16]/10 bg-white/55 p-3 shadow-[0_30px_90px_rgba(29,28,22,0.12)] backdrop-blur">
            <div className="overflow-hidden rounded-[1.5rem] border border-[#1d1c16]/10 bg-[#1d1c16]">
              <img
                src={HERO_GIF_PATH}
                alt="Claude Code UI 实时运行演示"
                className="h-auto w-full"
              />
            </div>
          </div>
        </div>
      </section>

      <section className="px-6 pb-16 sm:px-10 lg:px-16">
        <div className="mx-auto grid max-w-6xl gap-5 md:grid-cols-3">
          {featureCards.map((feature) => (
            <article
              key={feature.title}
              className="rounded-3xl border border-[#1d1c16]/10 bg-white/65 p-6 shadow-sm backdrop-blur"
            >
              <h2 className="text-xl font-semibold tracking-tight">{feature.title}</h2>
              <p className="mt-3 leading-7 text-[#5e5a50]">{feature.description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="px-6 pb-16 sm:px-10 lg:px-16">
        <div className="mx-auto max-w-6xl">
          <p className="mb-5 text-sm font-medium uppercase tracking-[0.22em] text-[#a44b2d]">
            实时运行截图
          </p>
          <div className="space-y-8">
            {RUN_SAMPLE_PATHS.map((sample, index) => (
              <figure
                key={sample}
                className="overflow-hidden rounded-3xl border border-[#1d1c16]/10 bg-white/70 p-2 shadow-[0_18px_60px_rgba(29,28,22,0.10)] backdrop-blur"
              >
                <img
                  src={sample}
                  alt={`Claude Code UI 实时运行截图 ${index + 1}`}
                  className="h-auto w-full rounded-[1.25rem]"
                />
              </figure>
            ))}
          </div>
        </div>
      </section>

      <section id="setup" className="px-6 pb-24 sm:px-10 lg:px-16">
        <div className="mx-auto max-w-6xl rounded-[2rem] border border-[#1d1c16]/10 bg-[#1d1c16] p-8 text-[#fbf9f6] shadow-[0_24px_80px_rgba(29,28,22,0.2)]">
          <p className="text-sm font-medium uppercase tracking-[0.22em] text-[#f0c7b2]">
            Local-first download
          </p>
          <h2 className="mt-4 text-3xl font-semibold tracking-tight sm:text-4xl">
            无需登录即可下载 Claude Code UI 源码
          </h2>
          <p className="mt-4 max-w-3xl leading-8 text-[#fbf9f6]/72">
            压缩包只包含 Claude Code UI 的源码、文档和锁文件；已排除本机个人配置、依赖目录与桌面安装包产物。
          </p>
          <a
            href={DOWNLOAD_PATH}
            download
            className="mt-8 inline-flex rounded-full bg-[#fbf9f6] px-7 py-4 font-medium text-[#1d1c16] transition hover:-translate-y-0.5 hover:bg-white"
          >
            下载源码包 cc-flow-src.zip
          </a>
        </div>
      </section>
    </main>
  );
}
