import { Outlet, useLocation, useMatch } from "react-router-dom"
import { AppSidebar } from "./app-sidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
} from "@/components/ui/breadcrumb"
import { TopicSwitcher } from "@/components/topics/topic-switcher"

const TITLES: Record<string, string> = {
  "/dashboard": "总览",
  "/topics": "专题",
  "/articles": "文章",
  "/accounts": "账号",
  "/jobs": "任务",
}

export function MainLayout() {
  const { pathname } = useLocation()
  const topicDetail = useMatch("/topics/:id")
  const title = topicDetail ? "专题详情" : TITLES[pathname] ?? "ai-ops-auto"

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 h-4" />
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem>
                <BreadcrumbLink href="/">ai-ops-auto</BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbItem>{title}</BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
          <div className="ml-auto">
            <TopicSwitcher />
          </div>
        </header>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}
