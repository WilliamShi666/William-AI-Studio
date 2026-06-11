import { flag } from 'flags/next';

export type IMaintenanceNotice =
  | {
      enabled: true;
      startTime: string; // Date
      endTime: string; // Date
    }
  | {
      enabled: false;
      startTime?: undefined;
      endTime?: undefined;
    };

export const maintenanceNoticeFlag = flag({
  key: 'maintenance-notice',
  async decide() {
    // 直接返回disabled状态，不依赖Vercel Edge Config
    return { enabled: false } as const;
  },
});
