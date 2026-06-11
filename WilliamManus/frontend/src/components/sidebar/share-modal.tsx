"use client"

import { useState, useEffect } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Copy, Share2, Link, Link2Off, Check, Globe, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { useThreadQuery, useUpdateThreadMutation, useUpdateProject } from "@/hooks/react-query"
import type { JSX } from "react"
import { Skeleton } from "../ui/skeleton"
import { useLanguage } from '@/contexts/LanguageContext';

interface SocialShareOption {
  name: string
  icon: React.ReactElement
  onClick: () => void
}

interface ShareModalProps {
  isOpen: boolean
  onClose: () => void
  threadId?: string
  projectId?: string
}

const ShareModalSkeleton = () => {
  return (
    <div className="space-y-6">
      <div className="rounded-lg border p-4">
        <div className="flex items-start space-x-2">
          <Skeleton className="h-4 w-4" />
          <div className="space-y-2 flex-1">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-full" />
            </div>
          </div>
        </div>
        <div className="space-y-2">
          <Skeleton className="h-4 w-20" />
          <div className="flex space-x-2">
            <Skeleton className="h-9 flex-1" />
            <Skeleton className="h-9 w-9" />
          </div>
        </div>
        <div className="space-y-3">
          <Skeleton className="h-4 w-24" />
          <div className="flex space-x-2">
            <Skeleton className="h-8 w-20" />
            <Skeleton className="h-8 w-16" />
          </div>
        </div>
        <Skeleton className="h-9 w-full" />
    </div>
  )
}

export function ShareModal({ isOpen, onClose, threadId, projectId }: ShareModalProps) {
  const [shareLink, setShareLink] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isCopying, setIsCopying] = useState(false)
  const { language } = useLanguage();
  const isZh = language === 'zh';
  const shareText = isZh ? '分享的对话' : 'Shared conversation';

  useEffect(() => {
    if (isOpen) {
      document.body.style.pointerEvents = "auto"
    }
  }, [isOpen])

  const updateThreadMutation = useUpdateThreadMutation()
  const updateProjectMutation = useUpdateProject()

  const { data: threadData, isLoading: isChecking } = useThreadQuery(threadId || "")

  useEffect(() => {
    if (threadData?.is_public) {
      const publicUrl = generateShareLink()
      setShareLink(publicUrl)
    } else {
      setShareLink(null)
    }
  }, [threadData])

  const generateShareLink = () => {
    if (!threadId) return ""
    return `${process.env.NEXT_PUBLIC_URL || window.location.origin}/share/${threadId}`
  }

  const createShareLink = async () => {
    if (!threadId) return

    setIsLoading(true)

    try {
      await updatePublicStatus(true)
      const generatedLink = generateShareLink()
      setShareLink(generatedLink)
      toast.success(isZh ? "分享链接已创建" : "Shareable link created successfully")
    } catch (error) {
      console.error("Error creating share link:", error)
      toast.error(isZh ? "创建分享链接失败" : "Failed to create shareable link")
    } finally {
      setIsLoading(false)
    }
  }

  const removeShareLink = async () => {
    if (!threadId) return

    setIsLoading(true)

    try {
      await updatePublicStatus(false)
      setShareLink(null)
      toast.success(isZh ? "分享链接已移除" : "Shareable link removed")
    } catch (error) {
      console.error("Error removing share link:", error)
      toast.error(isZh ? "移除分享链接失败" : "Failed to remove shareable link")
    } finally {
      setIsLoading(false)
    }
  }

  const updatePublicStatus = async (isPublic: boolean) => {
    if (!threadId || !projectId) return

    await updateProjectMutation.mutateAsync({
      projectId,
      data: { is_public: isPublic },
    })
    await updateThreadMutation.mutateAsync({
      threadId,
      data: { is_public: isPublic },
    })
  }

  const copyToClipboard = () => {
    if (shareLink) {
      setIsCopying(true)
      navigator.clipboard.writeText(shareLink)
      toast.success(isZh ? "链接已复制" : "Link copied to clipboard")
      setTimeout(() => {
        setIsCopying(false)
      }, 500)
    }
  }

  const socialOptions: SocialShareOption[] = [
    {
      name: "LinkedIn",
      icon: (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" className="h-4 w-4">
          <path
            fill="currentColor"
            d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"
          />
        </svg>
      ),
      onClick: () => {
        if (shareLink) {
          window.open(
            `https://www.linkedin.com/shareArticle?url=${encodeURIComponent(shareLink)}&text=${encodeURIComponent(shareText)}`,
            "_blank",
          )
        }
      },
    },
    {
      name: "X",
      icon: (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" className="h-4 w-4">
          <path
            fill="currentColor"
            d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z"
          />
        </svg>
      ),
      onClick: () => {
        if (shareLink) {
          window.open(
            `https://twitter.com/intent/tweet?url=${encodeURIComponent(shareLink)}&text=${encodeURIComponent(shareText)}`,
            "_blank",
          )
        }
      },
    },
  ]

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Share2 className="h-5 w-5" />
            {isZh ? '分享对话' : 'Share chat'}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6">
          {isChecking ? (
            <ShareModalSkeleton />
          ) : shareLink ? (
            <>
              <Alert>
                <Globe className="h-4 w-4" />
                <AlertDescription>
                  {isZh
                    ? '该对话已公开，拥有链接的任何人都可以查看。'
                    : 'This chat is publicly accessible. Anyone with the link can view this conversation.'}
                </AlertDescription>
              </Alert>

              <div className="space-y-2">
                <Label htmlFor="share-link">{isZh ? '分享链接' : 'Share link'}</Label>
                <div className="flex space-x-2">
                  <Input id="share-link" value={shareLink} readOnly className="font-mono text-sm" />
                  <Button variant="outline" size="icon" onClick={copyToClipboard} disabled={isCopying}>
                    {isCopying ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                    <span className="sr-only">{isZh ? '复制链接' : 'Copy link'}</span>
                  </Button>
                </div>
              </div>

              <div className="space-y-3">
                <Label>{isZh ? '分享到社交平台' : 'Share on social'}</Label>
                <div className="flex space-x-2">
                  {socialOptions.map((option, index) => (
                    <Button
                      key={index}
                      variant="outline"
                      size="sm"
                      onClick={option.onClick}
                      className="flex items-center"
                    >
                      {option.icon}
                      <span>{option.name}</span>
                    </Button>
                  ))}
                </div>
              </div>
              <Button
                variant="outline"
                className="w-full text-destructive hover:bg-destructive hover:text-muted"
                onClick={removeShareLink}
                disabled={isLoading}
              >
                <Link2Off className="h-4 w-4" />
                {isLoading ? (isZh ? "正在移除..." : "Removing...") : (isZh ? "移除链接" : "Remove link")}
              </Button>
            </>
          ) : (
            <div className="text-center space-y-4">
              <div className="mx-auto w-12 h-12 bg-muted-foreground/20 rounded-full flex items-center justify-center">
                <Share2 className="h-6 w-6" />
              </div>
              <div className="space-y-2">
                <h3 className="text-xl font-semibold">{isZh ? '分享此对话' : 'Share this chat'}</h3>
                <p className="text-sm text-muted-foreground">
                  {isZh
                    ? '创建可分享链接，让他人公开查看此对话。'
                    : 'Create a shareable link that allows others to view this conversation publicly.'}
                </p>
              </div>
              <Button onClick={createShareLink} disabled={isLoading} className="w-full">
                {isLoading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {isZh ? '正在创建...' : 'Creating...'}
                  </>
                ) : (
                  <>
                    <Link className="h-4 w-4" />
                    {isZh ? '创建分享链接' : 'Create shareable link'}
                  </>
                )}
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
