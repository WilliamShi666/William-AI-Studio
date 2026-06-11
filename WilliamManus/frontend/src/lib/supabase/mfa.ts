// 模拟的MFA多因子认证服务
// 在无认证环境下提供无操作的实现

export interface MFAEnrollResponse {
  data: any | null;
  error: any | null;
}

export interface MFAChallengeResponse {
  data: any | null;
  error: any | null;
}

export interface MFAVerifyResponse {
  data: any | null;
  error: any | null;
}

export interface MFAUnenrollResponse {
  data: any | null;
  error: any | null;
}

export interface MFALevelResponse {
  data: any | null;
  error: any | null;
}

// 模拟的MFA服务实现
export const supabaseMFAService = {
  // 注册MFA设备
  async enroll(params: {
    factorType: 'totp' | 'phone';
    issuer?: string;
    friendlyName?: string;
  }): Promise<MFAEnrollResponse> {
    console.log('Mock MFA enroll called - no authentication system');
    return {
      data: null,
      error: { message: 'MFA not available in local mode' }
    };
  },

  // 发起MFA挑战
  async challenge(params: {
    factorId: string;
  }): Promise<MFAChallengeResponse> {
    console.log('Mock MFA challenge called - no authentication system');
    return {
      data: null,
      error: { message: 'MFA not available in local mode' }
    };
  },

  // 验证MFA响应
  async verify(params: {
    factorId: string;
    challengeId: string;
    code: string;
  }): Promise<MFAVerifyResponse> {
    console.log('Mock MFA verify called - no authentication system');
    return {
      data: null,
      error: { message: 'MFA not available in local mode' }
    };
  },

  // 挑战并验证（一步完成）
  async challengeAndVerify(params: {
    factorId: string;
    code: string;
  }): Promise<MFAVerifyResponse> {
    console.log('Mock MFA challengeAndVerify called - no authentication system');
    return {
      data: null,
      error: { message: 'MFA not available in local mode' }
    };
  },

  // 获取已注册的MFA因子
  async listFactors(): Promise<MFAEnrollResponse> {
    console.log('Mock MFA listFactors called - no authentication system');
    return {
      data: [],
      error: null
    };
  },

  // 取消注册MFA设备
  async unenroll(params: {
    factorId: string;
  }): Promise<MFAUnenrollResponse> {
    console.log('Mock MFA unenroll called - no authentication system');
    return {
      data: null,
      error: { message: 'MFA not available in local mode' }
    };
  },

  // 获取认证保证级别
  async getAuthenticatorAssuranceLevel(): Promise<MFALevelResponse> {
    console.log('Mock MFA getAuthenticatorAssuranceLevel called - no authentication system');
    return {
      data: { currentLevel: null, nextLevel: null },
      error: null
    };
  },

  // 电话验证相关方法
  async sendPhoneChallenge(params: {
    phone: string;
  }): Promise<MFAChallengeResponse> {
    console.log('Mock phone challenge called - no authentication system');
    return {
      data: null,
      error: { message: 'Phone verification not available in local mode' }
    };
  },

  async verifyPhoneChallenge(params: {
    phone: string;
    token: string;
  }): Promise<MFAVerifyResponse> {
    console.log('Mock phone verify called - no authentication system');
    return {
      data: null,
      error: { message: 'Phone verification not available in local mode' }
    };
  }
}; 